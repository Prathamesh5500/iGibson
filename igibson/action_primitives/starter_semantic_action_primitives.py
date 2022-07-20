"""
WARNING!
The StarterSemanticActionPrimitive is a work-in-progress and is only provided as an example.
It currently only works with BehaviorRobot with its JointControllers set to absolute mode.
See provided behavior_robot_mp_behavior_task.yaml config file for an example. See examples/action_primitives for
runnable examples.
"""
import inspect
import logging
import random
from enum import IntEnum
from math import ceil

import gym
import numpy as np
import pybullet as p
from scipy.spatial.transform import Rotation

from igibson import object_states
from igibson.action_primitives.action_primitive_set_base import ActionPrimitiveError, BaseActionPrimitiveSet
from igibson.external.pybullet_tools.utils import set_joint_position
from igibson.object_states.on_floor import RoomFloor
from igibson.object_states.utils import get_center_extent, sample_kinematics
from igibson.objects.articulated_object import URDFObject
from igibson.objects.object_base import BaseObject
from igibson.robots import BaseRobot, behavior_robot
from igibson.robots.behavior_robot import DEFAULT_BODY_OFFSET_FROM_FLOOR, BehaviorRobot
from igibson.tasks.behavior_task import BehaviorTask
from igibson.utils.grasp_planning_utils import get_grasp_poses_for_object, get_grasp_position_for_open
from igibson.utils.motion_planning_utils import MotionPlanner
from igibson.utils.utils import restoreState

MAX_STEPS_FOR_HAND_MOVE = 100
MAX_STEPS_FOR_HAND_MOVE_WHEN_OPENING = 30
MAX_STEPS_FOR_GRASP_OR_RELEASE = 10
MAX_WAIT_FOR_GRASP_OR_RELEASE = 10
MAX_STEPS_FOR_WAYPOINT_NAVIGATION = 10

MAX_ATTEMPTS_FOR_SAMPLING_POSE_WITH_OBJECT_AND_PREDICATE = 20
MAX_ATTEMPTS_FOR_SAMPLING_POSE_NEAR_OBJECT = 60
MAX_ATTEMPTS_FOR_SAMPLING_POSE_IN_ROOM = 60

BIRRT_SAMPLING_CIRCLE_PROBABILITY = 0.5
HAND_SAMPLING_DOMAIN_PADDING = 1  # Allow 1m of freedom around the sampling range.
PREDICATE_SAMPLING_Z_OFFSET = 0.1
JOINT_CHECKING_RESOLUTION = np.pi / 18

GRASP_APPROACH_DISTANCE = 0.2
OPEN_GRASP_APPROACH_DISTANCE = 0.2
HAND_DISTANCE_THRESHOLD = 0.9 * behavior_robot.HAND_DISTANCE_THRESHOLD

ACTIVITY_RELEVANT_OBJECTS_ONLY = False

DEFAULT_DIST_THRESHOLD = 0.01
DEFAULT_ANGLE_THRESHOLD = 0.05

LOW_PRECISION_DIST_THRESHOLD = 0.1
LOW_PRECISION_ANGLE_THRESHOLD = 0.2

logger = logging.getLogger(__name__)


def indented_print(msg, *args, **kwargs):
    logger.debug("  " * len(inspect.stack()) + str(msg), *args, **kwargs)


def is_close(start_pose, end_pose, angle_threshold, dist_threshold):
    start_pos, start_orn = start_pose
    start_rot = Rotation.from_quat(start_orn)

    end_pos, end_orn = end_pose
    end_rot = Rotation.from_quat(end_orn)

    diff_rot = end_rot * start_rot.inv()
    diff_pos = np.array(end_pos) - np.array(start_pos)
    indented_print(
        "Position difference to target: %s, Rotation difference: %s", np.linalg.norm(diff_pos), diff_rot.magnitude()
    )

    return diff_rot.magnitude() < angle_threshold and np.linalg.norm(diff_pos) < dist_threshold


def pose_to_command(robot, joint_link, pose_in_body):
    shoulder_to_body = robot.links[joint_link].get_local_position_orientation()
    body_to_shoulder = p.invertTransform(*shoulder_to_body)
    pose_in_shoulder = p.multiplyTransforms(*body_to_shoulder, *pose_in_body)

    # Convert pos/quat to [x, y, z, rx, ry, rz]
    return np.concatenate([pose_in_shoulder[0], p.getEulerFromQuaternion(pose_in_shoulder[1])])


def behavior_robot_action_to_default_pose(robot: BehaviorRobot):
    parts_to_move_to_default_pos = [
        ("eye", "camera", "neck", behavior_robot.EYE_LOC_POSE_TRACKED),
        ("left_hand", "arm_left_hand", "left_hand_shoulder", behavior_robot.LEFT_HAND_LOC_POSE_TRACKED),
        ("right_hand", "arm_right_hand", "right_hand_shoulder", behavior_robot.RIGHT_HAND_LOC_POSE_TRACKED),
    ]
    # Compute the body information from the current frame.
    action = np.zeros(robot.action_dim)
    for part_name, controller_name, shoulder_name, target_pose in parts_to_move_to_default_pos:
        action[robot.controller_action_idx[controller_name]] = pose_to_command(robot, shoulder_name, target_pose)

    action[robot.controller_action_idx["gripper_right_hand"]] = 1.0

    return action


def get_still_action(robot: BehaviorRobot):
    action = np.zeros(robot.action_dim)
    # Compute the body information from the current frame.
    body = robot.base_link
    body_pose = body.get_position_orientation()
    world_frame_to_body_frame = p.invertTransform(*body_pose)
    parts_to_move_to_default_pos = [
        ("eye", "camera", "neck", robot.get_parts["eye"].get_local_position_orientation()),
        (
            "left_hand",
            "arm_left_hand",
            "left_hand_shoulder",
            robot.get_parts["left_hand"].get_local_position_orientation(),
        ),
        (
            "right_hand",
            "arm_right_hand",
            "right_hand_shoulder",
            robot.get_parts["right_hand"].get_local_position_orientation(),
        ),
    ]
    for part_name, controller_name, shoulder_name, target_pose in parts_to_move_to_default_pos:
        action[robot.controller_action_idx[controller_name]] = pose_to_command(robot, shoulder_name, target_pose)
    return action


def convert_behavior_robot_part_pose_to_action(
    robot: BehaviorRobot, body_target_pose=None, hand_target_pose=None, reset_others=True, low_precision=False
):
    assert body_target_pose is not None or hand_target_pose is not None

    # Compute the body information from the current frame.
    body = robot.base_link
    body_pose = body.get_position_orientation()
    world_frame_to_body_frame = p.invertTransform(*body_pose)

    # Accumulate the actions in the correct order.
    # We expect the action space to be absolute poses of body parts (not deltas) with respect to the robot base frame
    # If we reset them, we will overwrite the zeros
    # If we do not reset them, we need to query the current poses and keep still
    if reset_others:
        action = np.zeros(robot.action_dim)
    else:
        action = get_still_action(robot)

    part_close = {}
    dist_threshold = LOW_PRECISION_DIST_THRESHOLD if low_precision else DEFAULT_DIST_THRESHOLD
    angle_threshold = LOW_PRECISION_ANGLE_THRESHOLD if low_precision else DEFAULT_ANGLE_THRESHOLD

    # Compute the needed body motion
    if body_target_pose is not None:
        part_close["body"] = is_close(([0, 0, 0], [0, 0, 0, 1]), body_target_pose, dist_threshold, angle_threshold)
        action[robot.controller_action_idx["base"]] = pose_to_command(robot, robot.base_name, body_target_pose)

    # Keep a list of parts we'll move to default positions later. This is in correct order.
    parts_to_move_to_default_pos = [
        ("eye", "camera", "neck", behavior_robot.EYE_LOC_POSE_TRACKED),
        ("left_hand", "arm_left_hand", "left_hand_shoulder", behavior_robot.LEFT_HAND_LOC_POSE_TRACKED),
    ]

    # Take care of the right hand now.
    if hand_target_pose is not None:
        # Compute the needed right hand action
        right_hand = robot.eef_links["right_hand"]
        right_hand_pose_in_body_frame = p.multiplyTransforms(
            *world_frame_to_body_frame, *right_hand.get_position_orientation()
        )
        part_close["right_hand"] = is_close(
            right_hand_pose_in_body_frame, hand_target_pose, dist_threshold, angle_threshold
        )
        action[robot.controller_action_idx["arm_right_hand"]] = pose_to_command(
            robot, "right_hand_shoulder", hand_target_pose
        )
    else:
        # Move it back to the default position in with the below logic.
        parts_to_move_to_default_pos.append(
            ("right_hand", "arm_right_hand", "right_hand_shoulder", behavior_robot.RIGHT_HAND_LOC_POSE_TRACKED)
        )

    # Move other parts to default positions.
    if reset_others:
        for part_name, controller_name, shoulder_name, target_pose in parts_to_move_to_default_pos:
            part = robot.eef_links[part_name] if part_name != "eye" else robot.links["eyes"]
            part_pose_in_body_frame = p.multiplyTransforms(*world_frame_to_body_frame, *part.get_position_orientation())

            part_close[part_name] = is_close(part_pose_in_body_frame, target_pose, dist_threshold, angle_threshold)
            action[robot.controller_action_idx[controller_name]] = pose_to_command(robot, shoulder_name, target_pose)

    indented_print("What parts are close enough to their desired pose? %s", part_close)

    # Return None if no action is needed.
    if all(part_close.values()):
        return None

    return action


class UndoableContext(object):
    """
    Context that gets undone at the end.
    Whatever is done inside, is safe, meaning that when the context exits, the previous state is restored
    """

    def __init__(self, robot: BehaviorRobot):
        self.robot = robot

    def __enter__(self):
        self.robot_data = self.robot.dump_state()
        self.state = p.saveState()

    def __exit__(self, *args):
        self.robot.load_state(self.robot_data)
        restoreState(self.state)
        p.removeState(self.state)
        indented_print("Exiting UndoableContext context")


class StarterSemanticActionPrimitive(IntEnum):
    GRASP = 0
    PLACE_ON_TOP = 1
    PLACE_INSIDE = 2
    OPEN = 3
    CLOSE = 4
    NAVIGATE_TO = 5  # For mostly debugging purposes.


class StarterSemanticActionPrimitives(BaseActionPrimitiveSet):
    def __init__(self, env, task, scene, robot):
        logger.warning(
            "The StarterSemanticActionPrimitive is a work-in-progress and is only provided as an example. "
            "It currently only works with BehaviorRobot with its JointControllers set to absolute mode. "
            "See provided behavior_robot_mp_behavior_task.yaml config file for an example. "
            "See examples/action_primitives for runnable examples."
        )
        super().__init__(env, task, scene, robot)
        self.controller_functions = {
            StarterSemanticActionPrimitive.GRASP: self.grasp,
            StarterSemanticActionPrimitive.PLACE_ON_TOP: self.place_on_top,
            StarterSemanticActionPrimitive.PLACE_INSIDE: self.place_inside,
            StarterSemanticActionPrimitive.OPEN: self.open,
            StarterSemanticActionPrimitive.CLOSE: self.close,
            StarterSemanticActionPrimitive.NAVIGATE_TO: self._navigate_to_obj,
        }
        self.arm = "right_hand"
        full_observability_2d_planning = True
        collision_with_pb_2d_planning = True
        self.planner = MotionPlanner(
            self.env,
            optimize_iter=10,
            full_observability_2d_planning=full_observability_2d_planning,
            collision_with_pb_2d_planning=collision_with_pb_2d_planning,
            visualize_2d_planning=False,
            visualize_2d_result=False,
            fine_motion_plan=False,
        )

    def get_action_space(self):
        if ACTIVITY_RELEVANT_OBJECTS_ONLY:
            assert isinstance(self.task, BehaviorTask), "Activity relevant objects can only be used for BEHAVIOR tasks."
            self.addressable_objects = [
                item
                for item in self.task.object_scope.values()
                if isinstance(item, URDFObject) or isinstance(item, RoomFloor)
            ]
        else:
            self.addressable_objects = set(self.scene.objects_by_name.values())
            if isinstance(self.task, BehaviorTask):
                self.addressable_objects.update(self.task.object_scope.values())
            self.addressable_objects = list(self.addressable_objects)

        # Filter out the robots.
        self.addressable_objects = [obj for obj in self.addressable_objects if not isinstance(obj, BaseRobot)]

        self.num_objects = len(self.addressable_objects)
        return gym.spaces.Tuple(
            [gym.spaces.Discrete(self.num_objects), gym.spaces.Discrete(len(StarterSemanticActionPrimitive))]
        )

    def get_action_from_primitive_and_object(self, primitive: StarterSemanticActionPrimitive, obj: BaseObject):
        assert obj in self.addressable_objects
        primitive_int = int(primitive)
        return primitive_int, self.addressable_objects.index(obj)

    def _get_obj_in_hand(self):
        obj_in_hand_id = self.robot._ag_obj_in_hand[self.arm]  # TODO(MP): Expose this interface.
        obj_in_hand = self.scene.objects_by_id[obj_in_hand_id] if obj_in_hand_id is not None else None
        return obj_in_hand

    def apply(self, action):
        # Decompose the tuple
        action_idx, obj_idx = action

        # Find the target object.
        target_obj = self.addressable_objects[obj_idx]

        # Find the appropriate action generator.
        action = StarterSemanticActionPrimitive(action_idx)
        return self.controller_functions[action](target_obj)

    def open(self, obj):
        yield from self._open_or_close(obj, True)

    def close(self, obj):
        yield from self._open_or_close(obj, False)

    def reset(self):
        yield behavior_robot_action_to_default_pose(self.robot)

    def _open_or_close(self, obj, should_open):

        # Open the hand first
        yield from self._execute_release()

        # Don't do anything if the object is already closed and we're trying to close.
        if not should_open and not obj.states[object_states.Open].get_value():
            return

        grasp_data = get_grasp_position_for_open(self.robot, obj, should_open)
        if grasp_data is None:
            if should_open and obj.states[object_states.Open].get_value():
                # It's already open so we're good
                return
            else:
                # We were trying to do something but didn't have the data.
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.SAMPLING_ERROR,
                    "Could not sample grasp position for object.",
                    {"object": obj},
                )

        grasp_pose, target_poses, object_direction, joint_info, grasp_required = grasp_data
        with UndoableContext(self.robot):
            if self.planner.check_ee_collision(
                ee_position=grasp_pose[0],
                ee_orientation=grasp_pose[1],
                obj_in_hand=None,
                obstacles=self._get_collision_body_ids(include_robot=True),
                arm=self.arm,
            ):
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.SAMPLING_ERROR,
                    "Rejecting grasp pose due to collision.",
                    {"object": obj, "grasp_pose": grasp_pose},
                )

        # Prepare data for the approach later.
        approach_pos = grasp_pose[0] + object_direction * OPEN_GRASP_APPROACH_DISTANCE
        approach_pose = (approach_pos, grasp_pose[1])

        # If the grasp pose is too far, navigate
        [bid] = obj.get_body_ids()  # TODO: Fix this!
        check_joint = (bid, joint_info)
        yield from self._navigate_if_needed(obj, pos_on_obj=approach_pos, check_joint=check_joint)
        yield from self._navigate_if_needed(obj, pos_on_obj=grasp_pose[0], check_joint=check_joint)

        yield from self._move_hand(grasp_pose)

        # Since the grasp pose is slightly off the object, we want to move towards the object, around 5cm.
        # It's okay if we can't go all the way because we run into the object.
        indented_print("Performing grasp approach for open.")

        try:
            yield from self._move_hand_direct(approach_pose, ignore_failure=True, stop_on_contact=True)
        except ActionPrimitiveError:
            # An error will be raised when contact fails. If this happens, let's retreat back to the grasp pose.
            yield from self._move_hand_direct(grasp_pose, ignore_failure=True)
            raise

        if grasp_required:
            try:
                yield from self._execute_grasp()
            except ActionPrimitiveError:
                # Retreat back to the grasp pose.
                yield from self._execute_release()
                yield from self._move_hand_direct(grasp_pose, ignore_failure=True)
                raise

        for target_pose in target_poses:
            yield from self._move_hand_direct(
                target_pose, ignore_failure=True, max_steps_for_hand_move=MAX_STEPS_FOR_HAND_MOVE_WHEN_OPENING
            )

        # Moving to target pose often fails. Let's get the hand to apply the correct actions for its current pos
        # This prevents the hand from jerking into its desired position when we do a release.
        yield from self._move_hand_direct(
            self.robot.eef_links[self.arm].get_position_orientation(), ignore_failure=True
        )

        yield from self._execute_release()
        yield from self._reset_hand()

        if obj.states[object_states.Open].get_value() == should_open:
            return

    def grasp(self, obj):
        """
        Compute a viable grasp on the object (a pose), search for a trajectory to pregrasp and grasp and return the
        actions
        """
        # Don't do anything if the object is already grasped.
        obj_in_hand = self._get_obj_in_hand()
        if obj_in_hand is not None:
            if obj_in_hand == obj:
                logger.info("Object already grasped")
                return
            else:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.PRE_CONDITION_ERROR,
                    "Cannot grasp when hand is already full.",
                    {"object": obj, "object_in_hand": obj_in_hand},
                )

        # Allow grasping from suboptimal extents if we've tried enough times.
        force_allow_any_extent = np.random.rand() < 0.5
        grasp_poses = get_grasp_poses_for_object(self.robot, obj, force_allow_any_extent=force_allow_any_extent)
        grasp_pose, object_direction = random.choice(grasp_poses)

        # Check that the grasping pose is NOT in collision with the robot or the environment
        with UndoableContext(self.robot):
            if self.planner.check_ee_collision(
                ee_position=grasp_pose[0],
                ee_orientation=grasp_pose[1],
                obj_in_hand=None,
                obstacles=self._get_collision_body_ids(include_robot=True),
                arm=self.arm,
            ):
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.SAMPLING_ERROR,
                    "Rejecting grasp pose candidate due to collision",
                    {"grasp_pose": grasp_pose},  # TODO: Add more info about collision.
                )

        # Prepare data for the approach later.
        approach_pos = grasp_pose[0] + object_direction * GRASP_APPROACH_DISTANCE
        approach_pose = (approach_pos, grasp_pose[1])

        # If the grasp pose is too far, navigate.
        yield from self._navigate_if_needed(obj, pos_on_obj=approach_pos, detect_collisions_during_motion=True)
        yield from self._navigate_if_needed(obj, pos_on_obj=grasp_pose[0], detect_collisions_during_motion=True)

        yield from self._move_hand(grasp_pose, fail_on_contact=True)

        # Since the grasp pose is slightly off the object, we want to move towards the object, around 5cm.
        # It's okay if we can't go all the way because we run into the object.
        indented_print("Performing grasp approach.")
        yield from self._move_hand(approach_pose, ignore_failure=False, stop_on_contact=True)

        indented_print("Grasping.")
        yield from self._execute_grasp()

        indented_print("Moving hand back to neutral position.")
        yield from self._reset_hand_high()

        if self._get_obj_in_hand() == obj:
            indented_print("Successfully grasped the requested object!")
            return
        else:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Object was not grasped at the end.",
            )

    def place_on_top(self, obj):
        yield from self._place_with_predicate(obj, object_states.OnTop)

    def place_inside(self, obj):
        yield from self._place_with_predicate(obj, object_states.Inside)

    def toggle_on(self, obj):
        yield from self._toggle(obj, True)

    def toggle_off(self, obj):
        yield from self._toggle(obj, False)

    def _toggle(self, obj, value):
        if obj.states[object_states.ToggledOn].get_value() == value:
            return

        # Put the hand in the toggle marker.
        toggle_state = obj.states[object_states.ToggledOn]
        toggle_position = toggle_state.get_link_position()
        yield from self._navigate_if_needed(obj, toggle_position)

        hand_orientation = self.robot.eef_links[self.arm].get_orientation()  # Just keep the current hand orientation.
        desired_hand_pose = (toggle_position, hand_orientation)

        yield from self._move_hand_direct(desired_hand_pose, ignore_failure=True)

        # Put hand back where it was.
        yield from self._reset_hand()

        if obj.states[object_states.ToggledOn].get_value() != value:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR, "Failed to toggle object.", {"object": object}
            )

    def _place_with_predicate(self, obj, predicate):
        obj_in_hand = self._get_obj_in_hand()
        if obj_in_hand is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PRE_CONDITION_ERROR, "Cannot place object if not holding one."
            )

        obj_pose = self._sample_pose_with_object_and_predicate(predicate, obj_in_hand, obj)
        hand_pose = self._get_hand_pose_for_object_pose(obj_pose)

        indented_print("Navigating closer to the object to place on, if needed")
        yield from self._navigate_if_needed(
            obj, pos_on_obj=hand_pose[0], detect_collisions_during_motion=False, reset_others=False
        )

        indented_print("Moving hand to the placing pose")
        yield from self._move_hand(hand_pose)

        indented_print("Releasing the grasped object")
        yield from self._execute_release()

        indented_print("Resetting the hand")
        yield from self._reset_hand()

        if obj_in_hand.states[predicate].get_value(obj):
            indented_print("Success! The grasped object ended placed such that the given predicate is fulfilled!")
            return
        else:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Object was not placed in a way that the given predicate is fulfilled at the end.",
            )

    def _move_hand(self, target_pose, **kwargs):
        """
        Plan and execute a trajectory to move the hand to the given target pose (in world frame)
        """
        target_pose_in_correct_format = list(target_pose[0]) + list(p.getEulerFromQuaternion(target_pose[1]))

        # Define the sampling domain.
        cur_pos = np.array(self.robot.get_position())
        target_pos = np.array(target_pose[0])
        both_pos = np.array([cur_pos, target_pos])
        min_pos = np.min(both_pos, axis=0) - HAND_SAMPLING_DOMAIN_PADDING
        max_pos = np.max(both_pos, axis=0) + HAND_SAMPLING_DOMAIN_PADDING

        with UndoableContext(self.robot):
            plan = self.planner.plan_ee_motion_to_cartesian_pose(
                ee_position=target_pose[0], ee_orientation=target_pose[1], arm=self.arm
            )

        if plan is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR,
                "Could not make a hand motion plan.",
                {"target_pose": target_pose},
            )

        # Follow the plan to move the hand.
        indented_print("Plan has %d steps.", len(plan))
        initially_converged = True
        for i, xyz_rpy in enumerate(plan):
            try:
                indented_print("Executing grasp plan step %d/%d", i + 1, len(plan))
                pose = (xyz_rpy[:3], p.getQuaternionFromEuler(xyz_rpy[3:]))
                yield from self._move_hand_direct(pose, **kwargs)
                initially_converged = False
            except ActionPrimitiveError as ape:
                # Not for the first point
                if not initially_converged:
                    raise ape

    def _move_hand_direct(self, target_pose, **kwargs):
        yield from self._move_hand_direct_relative_to_robot(self._get_pose_in_robot_frame(target_pose), **kwargs)

    def _move_hand_direct_relative_to_robot(
        self,
        relative_target_pose,
        ignore_failure=False,
        max_steps_for_hand_move=MAX_STEPS_FOR_HAND_MOVE,
        stop_on_contact=False,
        fail_on_contact=False,
    ):
        """
        Move the hand to a given pose relative to the robot's torso/base frame
        """

        assert not (stop_on_contact and fail_on_contact), "Both options cannot be True at the same time"

        # Main loop. Try to move the hand to the desired relative pose for max_steps_for_hand_move simulation steps
        for _ in range(max_steps_for_hand_move):

            # Handling contact
            if p.getContactPoints(self.robot.eef_links[self.arm].body_id):  # TODO(MP): Generalize
                if stop_on_contact:
                    return
                elif fail_on_contact:
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.EXECUTION_ERROR,
                        "Contact was made in a motion that was expected to be collision free.",
                    )

            # Obtain the action to try to move the hand
            action = convert_behavior_robot_part_pose_to_action(self.robot, hand_target_pose=relative_target_pose)

            # The motion finished
            if action is None:
                # If we were expecting to contact an object before the end of the motion, but we didn't, we raise error
                if stop_on_contact:
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.EXECUTION_ERROR,
                        "No contact was made when contact was required as a move post-condition.",
                    )
                # Otherwise, return
                return

            # While the motion is not finished, we return the action to be executed
            yield action

        if not ignore_failure:
            current_pose = self.robot.get_relative_eef_pose()
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Could not move gripper to desired position.",
                {"relative_target_pose": relative_target_pose, "relative_current_pose": current_pose},
            )

        if stop_on_contact:
            raise ActionPrimitiveError(ActionPrimitiveError.Reason.EXECUTION_ERROR, "No contact was made.")

    def _get_still_action(self):
        return get_still_action(self.robot)

    def _execute_grasp(self):
        action = self._get_still_action()  # np.zeros(self.robot.action_dim)
        action[self.robot.controller_action_idx["gripper_right_hand"]] = -1.0
        for _ in range(MAX_STEPS_FOR_GRASP_OR_RELEASE):
            yield action

        # Do nothing for a bit so that AG can trigger.
        for _ in range(MAX_WAIT_FOR_GRASP_OR_RELEASE):
            yield self._get_still_action()  # np.zeros(self.robot.action_dim)

        if self._get_obj_in_hand() is None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "No object detected in hand after executing grasp.",
            )

    def _execute_release(self):
        action = self._get_still_action()  # np.zeros(self.robot.action_dim)
        action[self.robot.controller_action_idx["gripper_right_hand"]] = 1.0
        for _ in range(MAX_STEPS_FOR_GRASP_OR_RELEASE):
            # Otherwise, keep applying the action!
            yield action

        # Do nothing for a bit so that AG can trigger.
        for _ in range(MAX_WAIT_FOR_GRASP_OR_RELEASE):
            yield self._get_still_action()  # np.zeros(self.robot.action_dim)

        if self._get_obj_in_hand() is not None:
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.EXECUTION_ERROR,
                "Object still detected in hand after executing release.",
                {"object_in_hand": self._get_obj_in_hand()},
            )

    def _reset_hand(self):
        default_pose = p.multiplyTransforms(
            # TODO(MP): Generalize.
            *self.robot.get_position_orientation(),
            *behavior_robot.RIGHT_HAND_LOC_POSE_TRACKED,
        )
        yield from self._move_hand(default_pose)

    def _reset_hand_high(self):
        default_pose = p.multiplyTransforms(
            # TODO(MP): Generalize.
            *self.robot.get_position_orientation(),
            *behavior_robot.RIGHT_HAND_LOC_POSE_TRACKED_HIGH,
        )
        yield from self._move_hand(default_pose)

    def _rotate_in_place(self, yaw, low_precision=False):
        cur_pos = self.robot.get_position()
        target_pose = self._get_robot_pose_from_2d_pose((cur_pos[0], cur_pos[1], yaw))
        for _ in range(MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
            action = convert_behavior_robot_part_pose_to_action(
                self.robot, body_target_pose=self._get_pose_in_robot_frame(target_pose), low_precision=low_precision
            )
            if action is None:
                indented_print("Rotate is complete.")
                break

            yield action

    def _navigate_if_needed(
        self, obj, pos_on_obj=None, detect_collisions_during_motion=True, reset_others=True, **kwargs
    ):
        """
        Check if the BRobot needs to navigate to get closer to an object for a subsequent interaction
        If it is needed, compute a navigation path and execute it
        """
        indented_print("Navigating to get closer to a desired hand configuration")
        if pos_on_obj is not None:
            if self._get_dist_from_point_to_shoulder(pos_on_obj) < HAND_DISTANCE_THRESHOLD:
                # No need to navigate.
                indented_print("Robot's body is close enough to reach the desired hand configuration. Not moving")
                return
        elif obj.states[object_states.InReachOfRobot].get_value():
            indented_print("Robot's body is close enough to reach the desired hand configuration. Not moving")
            return

        indented_print("Robot's body is too far to reach the desired hand configuration. Moving it!")
        yield from self._navigate_to_obj(
            obj,
            pos_on_obj=pos_on_obj,
            detect_collisions_during_motion=detect_collisions_during_motion,
            reset_others=reset_others,
            **kwargs,
        )

    def _navigate_to_obj(self, obj, pos_on_obj=None, detect_collisions_during_motion=True, reset_others=True, **kwargs):
        """
        Sample a valid location next to the given object (the object can be a room), compute a navigation path and
          execute it
        """
        indented_print("Navigating towards an object ({})".format(obj.name))
        if isinstance(obj, RoomFloor):
            # TODO(lowprio-MP): Pos-on-obj for the room navigation?
            indented_print("The goal is a room. Sampling a valid location in the room")
            pose = self._sample_pose_in_room(obj.room_instance)
        else:
            indented_print("The goal is not a room. Sampling a valid location near the object")
            pose = self._sample_pose_near_object(obj, pos_on_obj=pos_on_obj, **kwargs)

        yield from self._navigate_to_pose(
            pose, detect_collisions_during_motion=detect_collisions_during_motion, reset_others=reset_others
        )

    def _navigate_to_pose(self, pose_2d, detect_collisions_during_motion=True, reset_others=True):
        """
        Compute a 2D path and execute it to move the BRobot to the given 2D location (actually, it is 3D because of
          the orientation)
        """
        indented_print("Navigating the robot to a given location")
        with UndoableContext(self.robot):
            # Note that the plan returned by this planner only contains xy pairs & not yaw.
            plan = self.planner.plan_base_motion(
                goal=pose_2d,
            )

        if plan is None:
            # TODO: Would be great to produce a more informative error.
            raise ActionPrimitiveError(
                ActionPrimitiveError.Reason.PLANNING_ERROR, "Planner failed: could not find a navigation path."
            )

        # Follow the plan to navigate.
        indented_print("Plan has %d steps.", len(plan))
        for i, pose_2d in enumerate(plan):
            indented_print("Executing navigation plan step %d/%d", i + 1, len(plan))
            low_precision = True if i < len(plan) - 1 else False
            yield from self._navigate_to_pose_direct(pose_2d, low_precision=low_precision, reset_others=reset_others)
            if detect_collisions_during_motion:
                if self._detect_robot_collision():
                    raise ActionPrimitiveError(
                        ActionPrimitiveError.Reason.EXECUTION_ERROR, "Detected collision during navigation."
                    )

    def _navigate_to_pose_direct(self, pose_2d, low_precision=False, reset_others=True):
        """
        Create an action to displace the entire BRobot to the given 2D location (with orientation)
        """
        # Keep the same orientation until the target.
        pose = self._get_robot_pose_from_2d_pose(pose_2d)
        for _ in range(MAX_STEPS_FOR_WAYPOINT_NAVIGATION):
            action = convert_behavior_robot_part_pose_to_action(
                self.robot,
                body_target_pose=self._get_pose_in_robot_frame(pose),
                low_precision=low_precision,
                reset_others=reset_others,
            )
            if action is None:
                indented_print("Move is complete.")
                return

            yield action

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.EXECUTION_ERROR,
            "Could not move robot to desired waypoint.",
            {"target_pose": pose, "current_pose": self.robot.get_position_orientation()},
        )

    def _sample_pose_near_object(self, obj, pos_on_obj=None, **kwargs):
        indented_print("Sampling a valid location for the base next to the the given object ({})".format(obj.name))
        if pos_on_obj is None:
            pos_on_obj = self._sample_position_on_aabb_face(obj)

        pos_on_obj = np.array(pos_on_obj)
        obj_rooms = obj.in_rooms if obj.in_rooms else [self.scene.get_room_instance_by_point(pos_on_obj[:2])]
        for _ in range(MAX_ATTEMPTS_FOR_SAMPLING_POSE_NEAR_OBJECT):
            distance = np.random.uniform(0.2, 1.0)
            yaw = np.random.uniform(-np.pi, np.pi)
            pose_2d = np.array(
                [pos_on_obj[0] + distance * np.cos(yaw), pos_on_obj[1] + distance * np.sin(yaw), yaw + np.pi]
            )

            # Check room
            if self.scene.get_room_instance_by_point(pose_2d[:2]) not in obj_rooms:
                indented_print("Candidate position is in the wrong room.")
                continue

            if not self._test_pose(pose_2d, pos_on_obj=pos_on_obj, **kwargs):
                indented_print("Robot location not valid. Continue searching for a valid location next to the object")
                continue

            indented_print("Found valid robot location next to the object: {}".format(pose_2d))
            return pose_2d

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.SAMPLING_ERROR, "Could not find valid position near object."
        )

    @staticmethod
    def _sample_position_on_aabb_face(target_obj):
        aabb_center, aabb_extent = get_center_extent(target_obj.states)
        # We want to sample only from the side-facing faces.
        face_normal_axis = random.choice([0, 1])
        face_normal_direction = random.choice([-1, 1])
        face_center = aabb_center + np.eye(3)[face_normal_axis] * aabb_extent * face_normal_direction
        face_lateral_axis = 0 if face_normal_axis == 1 else 1
        face_lateral_half_extent = np.eye(3)[face_lateral_axis] * aabb_extent / 2
        face_vertical_half_extent = np.eye(3)[2] * aabb_extent / 2
        face_min = face_center - face_vertical_half_extent - face_lateral_half_extent
        face_max = face_center + face_vertical_half_extent + face_lateral_half_extent
        return np.random.uniform(face_min, face_max)

    def _sample_pose_in_room(self, room: str):
        # TODO(MP): Bias the sampling near the agent.
        for _ in range(MAX_ATTEMPTS_FOR_SAMPLING_POSE_IN_ROOM):
            _, pos = self.scene.get_random_point_by_room_instance(room)
            yaw = np.random.uniform(-np.pi, np.pi)
            pose = (pos[0], pos[1], yaw)
            if self._test_pose(pose):
                return pose

        raise ActionPrimitiveError(
            ActionPrimitiveError.Reason.SAMPLING_ERROR, "Could not find valid position in room.", {"room": room}
        )

    def _sample_pose_with_object_and_predicate(self, predicate, held_obj, target_obj):
        with UndoableContext(self.robot):
            pred_map = {object_states.OnTop: "onTop", object_states.Inside: "inside"}
            result = sample_kinematics(
                pred_map[predicate],
                held_obj,
                target_obj,
                True,
                use_ray_casting_method=True,
                max_trials=MAX_ATTEMPTS_FOR_SAMPLING_POSE_WITH_OBJECT_AND_PREDICATE,
                skip_falling=True,
                z_offset=PREDICATE_SAMPLING_Z_OFFSET,
            )

            if not result:
                raise ActionPrimitiveError(
                    ActionPrimitiveError.Reason.SAMPLING_ERROR,
                    "Could not sample position with object and predicate.",
                    {"target_object": target_obj, "held_object": held_obj, "predicate": pred_map[predicate]},
                )

            pos, orn = held_obj.get_position_orientation()
            return pos, orn

    @staticmethod
    def _detect_collision(body, obj_in_hand=None):
        collision = []
        obj_in_hand_id = None
        if obj_in_hand is not None:
            [obj_in_hand_id] = obj_in_hand.get_body_ids()
        for body_id in range(p.getNumBodies()):
            if body_id == body or body_id == obj_in_hand_id:
                continue
            closest_points = p.getClosestPoints(body, body_id, distance=0.01)
            if len(closest_points) > 0:
                collision.append(body_id)
                break
        return collision

    def _detect_robot_collision(self):
        # TODO(MP): Generalize.
        indented_print("Start collision test.")
        body = self._detect_collision(self.robot.links["body"].body_id)
        if body:
            bodyInfo = p.getBodyInfo(body[0])
            indented_print("Body has collision with objects {}".format(bodyInfo[0].decode("ascii")))
        left = self._detect_collision(self.robot.eef_links["left_hand"].body_id)
        if left:
            bodyInfo = p.getBodyInfo(left[0])
            indented_print("Left hand has collision with objects {}".format(bodyInfo[0].decode("ascii")))
        right = self._detect_collision(self.robot.eef_links[self.arm].body_id, self._get_obj_in_hand())
        if right:
            bodyInfo = p.getBodyInfo(right[0])
            indented_print("Right hand has collision with objects {}".format(bodyInfo[0].decode("ascii")))
        indented_print("End collision test.")

        return body or left or right

    def _test_pose(self, pose_2d, pos_on_obj=None, check_joint=None):
        with UndoableContext(self.robot):
            self.robot.set_position_orientation(*self._get_robot_pose_from_2d_pose(pose_2d))

            if pos_on_obj is not None:
                hand_distance = self._get_dist_from_point_to_shoulder(pos_on_obj)
                if hand_distance > HAND_DISTANCE_THRESHOLD:
                    indented_print("Candidate position failed shoulder distance test.")
                    return False

            if self._detect_robot_collision():
                indented_print("Candidate position failed collision test.")
                return False

            if check_joint is not None:
                body_id, joint_info = check_joint

                # Check at different positions of the joint.
                joint_range = joint_info.jointUpperLimit - joint_info.jointLowerLimit
                turn_steps = int(ceil(abs(joint_range) / JOINT_CHECKING_RESOLUTION))
                for i in range(turn_steps):
                    joint_pos = (i + 1) / turn_steps * joint_range + joint_info.jointLowerLimit
                    set_joint_position(body_id, joint_info.jointIndex, joint_pos)

                    if self._detect_robot_collision():
                        indented_print("Candidate position failed joint-move collision test.")
                        return False

            return True

    @staticmethod
    def _get_robot_pose_from_2d_pose(pose_2d):
        pos = np.array([pose_2d[0], pose_2d[1], DEFAULT_BODY_OFFSET_FROM_FLOOR])
        orn = p.getQuaternionFromEuler([0, 0, pose_2d[2]])
        return pos, orn

    def _get_pose_in_robot_frame(self, pose):
        body_pose = self.robot.get_position_orientation()
        world_to_body_frame = p.invertTransform(*body_pose)
        relative_target_pose = p.multiplyTransforms(*world_to_body_frame, *pose)
        return relative_target_pose

    def _get_collision_body_ids(self, include_robot=False):
        ids = []
        for obj in self.scene.get_objects():
            if not isinstance(obj, BaseRobot):
                ids.extend(obj.get_body_ids())

        if include_robot:
            # TODO(MP): Generalize
            ids.append(self.robot.eef_links["left_hand"].body_id)
            ids.append(self.robot.base_link.body_id)

        return ids

    def _get_dist_from_point_to_shoulder(self, pos):
        # TODO(MP): Generalize
        shoulder_pos_in_base_frame = np.array(
            self.robot.links["%s_shoulder" % self.arm].get_local_position_orientation()[0]
        )
        point_in_base_frame = np.array(self._get_pose_in_robot_frame((pos, [0, 0, 0, 1]))[0])
        shoulder_to_hand = point_in_base_frame - shoulder_pos_in_base_frame
        return np.linalg.norm(shoulder_to_hand)

    def _get_hand_pose_for_object_pose(self, desired_pose):
        obj_in_hand = self._get_obj_in_hand()

        assert obj_in_hand is not None

        # Get the object pose & the robot hand pose
        obj_in_world = obj_in_hand.get_position_orientation()
        hand_in_world = self.robot.eef_links[self.arm].get_position_orientation()

        # Get the hand pose relative to the obj pose
        world_in_obj = p.invertTransform(*obj_in_world)
        hand_in_obj = p.multiplyTransforms(*world_in_obj, *hand_in_world)

        # Now apply desired obj pose.
        desired_hand_pose = p.multiplyTransforms(*desired_pose, *hand_in_obj)

        return desired_hand_pose
