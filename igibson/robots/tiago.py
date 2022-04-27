import os

import numpy as np
import pybullet as p

import igibson
from igibson.controllers import ControlType
from igibson.robots.active_camera_robot import ActiveCameraRobot
from igibson.robots.locomotion_robot import LocomotionRobot
from igibson.robots.manipulation_robot import GraspingPoint, ManipulationRobot
from igibson.robots.robot_base import VirtualJoint
from igibson.utils.constants import SemanticClass
from igibson.utils.python_utils import assert_valid_key

BODY_LINEAR_VELOCITY = 0.05  # linear velocity thresholds in meters/frame
BODY_ANGULAR_VELOCITY = 0.05  # angular velocity thresholds in radians/frame

# Body parameters
BODY_MOVING_FORCE = 300

DEFAULT_ARM_POSES = {
    "vertical",
    "diagonal15",
    "diagonal30",
    "diagonal45",
    "horizontal",
}

RESET_JOINT_OPTIONS = {
    "tuck",
    "untuck",
}


class Tiago(ManipulationRobot, LocomotionRobot, ActiveCameraRobot):
    """
    Tiago Robot
    Reference: https://pal-robotics.com/robots/tiago/
    """

    def __init__(
        self,
        reset_joint_pos=None,
        rigid_trunk=False,
        default_trunk_offset=0.365,
        default_arm_pose="vertical",
        **kwargs,
    ):
        """
        :param reset_joint_pos: None or str or Array[float], if specified, should be the joint positions that the robot
            should be set to during a reset. If str, should be one of {tuck, untuck}, corresponds to default
            configurations for un/tucked modes. If None (default), self.default_joint_pos (untuck mode) will be used
            instead.
        :param rigid_trunk: bool, if True, will prevent the trunk from moving during execution.
        :param default_trunk_offset: float, sets the default height of the robot's trunk
        :param default_arm_pose: Default pose for the robot arm. Should be one of {"vertical", "diagonal15",
            "diagonal30", "diagonal45", "horizontal"}
        :param **kwargs: see ManipulationRobot, TwoWheelRobot, ActiveCameraRobot
        """
        # Store args
        self.rigid_trunk = rigid_trunk
        self.default_trunk_offset = default_trunk_offset
        assert_valid_key(key=default_arm_pose, valid_keys=DEFAULT_ARM_POSES, name="default_arm_pose")
        self.default_arm_pose = default_arm_pose

        self.new_pos = [0, 0, 0]
        self.new_orn = [0, 0, 0, 1]
        self.movement_cid = None

        # Parse reset joint pos if specifying special string
        if isinstance(reset_joint_pos, str):
            assert (
                reset_joint_pos in RESET_JOINT_OPTIONS
            ), "reset_joint_pos should be one of {} if using a string!".format(RESET_JOINT_OPTIONS)
            reset_joint_pos = (
                self.tucked_default_joint_pos if reset_joint_pos == "tuck" else self.untucked_default_joint_pos
            )

        # Run super init
        super().__init__(reset_joint_pos=reset_joint_pos, **kwargs)

    @property
    def model_name(self):
        """
        :return str: robot model name
        """
        return "Tiago"

    @property
    def n_arms(self):
        return 2

    @property
    def arm_names(self):
        return ["left", "right"]

    @property
    def tucked_default_joint_pos(self):
        return np.array(
            [0.1]
            + [0] * 2
            + [-1.17, 1.57, 2.74, 1.57, 1.57, -1.41, 0.0]
            + [0] * 2
            + [-1.17, 1.57, 2.74, 1.57, 1.57, -1.41, 0.0]
            + [0] * 2
        )

    @property
    def untucked_default_joint_pos(self):
        pos = np.zeros(self.n_joints)
        # pos[self.base_control_idx] = 0.0
        pos[self.trunk_control_idx] = 0.02 + self.default_trunk_offset
        pos[self.camera_control_idx] = np.array([-1.0, 0.0])
        for arm in self.arm_names:
            pos[self.gripper_control_idx[arm]] = np.array([0.05, 0.05])  # open gripper

            # Choose arm based on setting
            if self.default_arm_pose == "vertical":
                pos[self.arm_control_idx[arm]] = np.array(
                    # [-0.94121, -0.64134, 1.55186, 1.65672, -0.93218, 1.53416, 2.14474]
                    [0.22, 0.48, 1.52, 1.76, 0.04, -0.49, 0]
                )
            elif self.default_arm_pose == "diagonal15":
                pos[self.arm_control_idx[arm]] = np.array(
                    # [-0.95587, -0.34778, 1.46388, 1.47821, -0.93813, 1.4587, 1.9939]
                    [0.22, 0.48, 1.52, 1.76, 0.04, -0.49, 0]
                )
            elif self.default_arm_pose == "diagonal30":
                pos[self.arm_control_idx[arm]] = np.array(
                    # [-1.06595, -0.22184, 1.53448, 1.46076, -0.84995, 1.36904, 1.90996]
                    [0.22, 0.48, 1.52, 1.76, 0.04, -0.49, 0]
                )
            elif self.default_arm_pose == "diagonal45":
                pos[self.arm_control_idx[arm]] = np.array(
                    # [-1.11479, -0.0685, 1.5696, 1.37304, -0.74273, 1.3983, 1.79618]
                    [0.22, 0.48, 1.52, 1.76, 0.04, -0.49, 0]
                )
            elif self.default_arm_pose == "horizontal":
                pos[self.arm_control_idx[arm]] = np.array(
                    # [-1.43016, 0.20965, 1.86816, 1.77576, -0.27289, 1.31715, 2.01226]
                    [0.22, 0.48, 1.52, 1.76, 0.04, -0.49, 0]
                )
            else:
                raise ValueError("Unknown default arm pose: {}".format(self.default_arm_pose))
        return pos

    def _create_discrete_action_space(self):
        # Tiago does not support discrete actions
        raise ValueError("Tiago does not support discrete actions!")

    def tuck(self):
        """
        Immediately set this robot's configuration to be in tucked mode
        """
        self.set_joint_positions(self.tucked_default_joint_pos)

    def untuck(self):
        """
        Immediately set this robot's configuration to be in untucked mode
        """
        self.set_joint_positions(self.untucked_default_joint_pos)

    def load(self, simulator):
        # Run super method
        ids = super().load(simulator)

        assert len(ids) == 1, "Tiago robot is expected to have only one body ID."

        # Extend super method by increasing laterial friction for EEF
        for arm in self.arm_names:
            for link in self.finger_joint_ids[arm]:
                p.changeDynamics(self.base_link.body_id, link, lateralFriction=500)

        self.tuck()

        return ids

    def _setup_virtual_joints(self):
        virtual_joints = []
        virtual_joints.extend(
            VirtualPlanarJoint(
                joint_name="body__body",
                parent_link=self.base_link,
                child_link=self.base_link,
                command_callback=self.base_command_position,
                reset_callback=self.base_reset_position,
                lower_limits=[None, None, None, None, None, None],
                upper_limits=[None, None, None, None, None, None],
            ).get_joints()
        )
        return virtual_joints

    def move_constraints(self, pos, orn):
        if self.movement_cid:
            # orn = [0, 0, 0, 1]
            p.changeConstraint(self.movement_cid, pos, orn, maxForce=BODY_MOVING_FORCE)
            # print(pos, orn)
        else:
            self.movement_cid = p.createConstraint(
                parentBodyUniqueId=self.base_link.body_id,
                parentLinkIndex=-1,
                childBodyUniqueId=-1,
                childLinkIndex=-1,
                jointType=p.JOINT_FIXED,
                jointAxis=[0, 0, 1],
                parentFramePosition=[0, 0, 0],
                childFramePosition=self.get_position(),
                parentFrameOrientation=[0, 0, 0, 1],
                childFrameOrientation=self.get_orientation(),
            )

    def base_command_position(self, action):
        """
        Updates Tiago to new position and rotation, via constraints.
        :param action: numpy array of actions.
        """
        # Compute the target world position from the delta position.
        action[:3] = np.clip(action[:3], -BODY_LINEAR_VELOCITY, BODY_LINEAR_VELOCITY)
        action[3:] = np.clip(action[3:], -BODY_ANGULAR_VELOCITY, BODY_ANGULAR_VELOCITY)
        delta_pos, delta_orn = action[:3], p.getQuaternionFromEuler(action[3:6])
        target_pos, target_orn = p.multiplyTransforms(*self.get_position_orientation(), delta_pos, delta_orn)

        self.new_pos = np.round(target_pos, 5).tolist()
        self.new_orn = np.round(target_orn, 5).tolist()

        self.move_constraints(self.new_pos, self.new_orn)

    def base_reset_position(self, reset_val):
        """
        Reset Tiago to new position and rotation, via teleportation.
        :param reset_val: numpy array of joint values to reset
        """
        # Compute the target world position from the delta position.
        delta_pos, delta_orn = reset_val[:3], p.getQuaternionFromEuler(reset_val[3:6])
        target_pos, target_orn = p.multiplyTransforms(*self.get_position_orientation(), delta_pos, delta_orn)

        # Clip the height.
        target_pos = [target_pos[0], target_pos[1], np.clip(target_pos[2], BODY_HEIGHT_RANGE[0], BODY_HEIGHT_RANGE[1])]

        self.new_pos = np.round(self.new_pos, 5).tolist()
        self.new_orn = np.round(self.new_orn, 5).tolist()

        self.set_position_orientation(self.new_pos, self.new_orn)

    def get_position_orientation(self):
        """Get object position and orientation in the format of Tuple[Array[x, y, z], Array[x, y, z, w]]"""
        pos, orn = p.getBasePositionAndOrientation(self.base_link.body_id)
        return np.array(pos), np.array(orn)

    def set_position_orientation(self, pos, orn):
        # super(Tiago, self).set_position_orientation(pos, orn)
        self.new_pos = pos
        self.new_orn = orn
        p.resetBasePositionAndOrientation(self.base_link.body_id, pos, orn)
        self.move_constraints(pos, orn)

    def command_reset(self, val):
        if val > 0.5:  # The unnormalized action space for this button is 0 to 1. This thresholds that space into half.
            self.set_position_orientation(self.new_pos, self.new_orn)

    def reset(self):
        # Move the constraint for each part to the default position.
        self.set_position_orientation(*self.get_position_orientation())

    def _actions_to_control(self, action):
        # Run super method first
        u_vec, u_type_vec = super()._actions_to_control(action=action)

        # Override trunk value if we're keeping the trunk rigid
        if self.rigid_trunk:
            u_vec[self.trunk_control_idx] = self.untucked_default_joint_pos[self.trunk_control_idx]
            u_type_vec[self.trunk_control_idx] = ControlType.POSITION

        # Return control
        return u_vec, u_type_vec

    def _get_proprioception_dict(self):
        dic = super()._get_proprioception_dict()

        # Add trunk info
        dic["trunk_qpos"] = self.joint_positions[self.trunk_control_idx]
        dic["trunk_qvel"] = self.joint_velocities[self.trunk_control_idx]

        return dic

    @property
    def default_proprio_obs(self):
        obs_keys = super().default_proprio_obs
        return obs_keys + ["trunk_qpos"]

    @property
    def controller_order(self):
        controllers = ["base", "camera"]
        for arm in self.arm_names:
            controllers += ["arm_{}".format(arm), "gripper_{}".format(arm)]

        return controllers

    @property
    def _default_controllers(self):
        # Always call super first
        controllers = super()._default_controllers

        # We use multi finger gripper, differential drive, and IK controllers as default
        controllers["base"] = "JointController"
        controllers["camera"] = "JointController"
        for arm in self.arm_names:
            controllers["arm_{}".format(arm)] = "InverseKinematicsController"
            controllers["gripper_{}".format(arm)] = "MultiFingerGripperController"

        return controllers

    @property
    def _default_base_controller_configs(self):
        dic = {
            "name": "JointController",
            "control_freq": self.control_freq,
            "control_limits": self.control_limits,
            "use_delta_commands": False,
            "motor_type": "position",
            "compute_delta_in_quat_space": [(3, 4, 5)],
            "joint_idx": self.base_control_idx,
            "command_input_limits": (
                np.array([-BODY_LINEAR_VELOCITY] * 3 + [-BODY_ANGULAR_VELOCITY] * 3),
                np.array([BODY_LINEAR_VELOCITY] * 3 + [BODY_ANGULAR_VELOCITY] * 3),
            ),
            "command_output_limits": None,
        }
        return dic

    @property
    def _default_controller_config(self):
        # Grab defaults from super method first
        cfg = super()._default_controller_config
        cfg["base"] = {"JointController": self._default_base_controller_configs}
        for arm in self.arm_names:
            # Use default IK controller -- also need to override joint idx being controlled to include trunk in default
            # IK arm controller
            cfg["arm_{}".format(arm)]["InverseKinematicsController"]["joint_idx"] = np.concatenate(
                [self.trunk_control_idx, self.arm_control_idx[self.default_arm]]
            )

            # If using rigid trunk, we also clamp its limits
            if self.rigid_trunk:
                cfg["arm_{}".format(arm)]["InverseKinematicsController"]["control_limits"]["position"][0][
                    self.trunk_control_idx
                ] = self.untucked_default_joint_pos[self.trunk_control_idx]
                cfg["arm_{}".format(arm)]["InverseKinematicsController"]["control_limits"]["position"][1][
                    self.trunk_control_idx
                ] = self.untucked_default_joint_pos[self.trunk_control_idx]

        return cfg

    @property
    def control_limits(self):
        return {"position": (self.joint_lower_limits, self.joint_upper_limits), "has_limit": self.joint_has_limits}

    @property
    def default_joint_pos(self):
        return self.untucked_default_joint_pos

    @property
    def wheel_radius(self):
        return 0.0613

    @property
    def wheel_axle_length(self):
        return 0.372

    @property
    def gripper_link_to_grasp_point(self):
        return {self.default_arm: np.array([0.1, 0, 0])}

    @property
    def assisted_grasp_start_points(self):
        return {
            arm: [
                GraspingPoint(link_name="gripper_{}_right_finger_link".format(arm), position=[0.04, -0.012, 0.014]),
                GraspingPoint(link_name="gripper_{}_right_finger_link".format(arm), position=[0.04, -0.012, -0.014]),
                GraspingPoint(link_name="gripper_{}_right_finger_link".format(arm), position=[-0.04, -0.012, 0.014]),
                GraspingPoint(link_name="gripper_{}_right_finger_link".format(arm), position=[-0.04, -0.012, -0.014]),
            ]
            for arm in self.arm_names
        }

    @property
    def assisted_grasp_end_points(self):
        return {
            arm: [
                GraspingPoint(link_name="gripper_{}_left_finger_link".format(arm), position=[0.04, 0.012, 0.014]),
                GraspingPoint(link_name="gripper_{}_left_finger_link".format(arm), position=[0.04, 0.012, -0.014]),
                GraspingPoint(link_name="gripper_{}_left_finger_link".format(arm), position=[-0.04, 0.012, 0.014]),
                GraspingPoint(link_name="gripper_{}_left_finger_link".format(arm), position=[-0.04, 0.012, -0.014]),
            ]
            for arm in self.arm_names
        }

    @property
    def base_control_idx(self):
        joints = list(self.joints.keys())
        return np.array(
            [
                joints.index("body__body_%s" % (component))
                for component in VirtualPlanarJoint.ACTUATED_COMPONENT_SUFFIXES
            ]
        )

    @property
    def trunk_control_idx(self):
        """
        :return Array[int]: Indices in low-level control vector corresponding to trunk joint.
        """
        return np.array([0])

    @property
    def camera_control_idx(self):
        """
        :return Array[int]: Indices in low-level control vector corresponding to [tilt, pan] camera joints.
        """
        return np.array([2, 1])

    @property
    def arm_control_idx(self):
        """
        :return dict[str, Array[int]]: Dictionary mapping arm appendage name to indices in low-level control
            vector corresponding to arm joints.
        """
        return {"left": np.array([3, 4, 5, 6, 7, 8, 9]), "right": np.array([12, 13, 14, 15, 16, 17, 18])}

    @property
    def gripper_control_idx(self):
        """
        :return dict[str, Array[int]]: Dictionary mapping arm appendage name to indices in low-level control
            vector corresponding to gripper joints.
        """
        return {"left": np.array([10, 11]), "right": np.array([19, 20])}

    @property
    def disabled_collision_pairs(self):
        return [
            ['arm_left_1_link', 'arm_left_2_link'],
            ['arm_left_2_link', 'arm_left_3_link'],
            ['arm_left_3_link', 'arm_left_4_link'],
            ['arm_left_4_link', 'arm_left_5_link'],
            ['arm_left_5_link', 'arm_left_6_link'],
            ['arm_left_6_link', 'arm_left_7_link'],
            ['arm_right_1_link', 'arm_right_2_link'],
            ['arm_right_2_link', 'arm_right_3_link'],
            ['arm_right_3_link', 'arm_right_4_link'],
            ['arm_right_4_link', 'arm_right_5_link'],
            ['arm_right_5_link', 'arm_right_6_link'],
            ['arm_right_6_link', 'arm_right_7_link'],
        ]

    @property
    def eef_link_names(self):
        return {arm: "gripper_{}_link".format(arm) for arm in self.arm_names}

    @property
    def finger_link_names(self):
        return {
            arm: ["gripper_{}_right_finger_link".format(arm), "gripper_{}_left_finger_link".format(arm)]
            for arm in self.arm_names
        }

    @property
    def finger_joint_names(self):
        return {
            arm: ["gripper_{}_right_finger_joint".format(arm), "gripper_{}_left_finger_joint".format(arm)]
            for arm in self.arm_names
        }

    @property
    def model_file(self):
        return os.path.join(igibson.assets_path, "models/tiago/tiago_dual_omnidirectional_stanford.urdf")

    def dump_config(self):
        """Dump robot config"""
        dump = super(Tiago, self).dump_config()
        dump.update(
            {
                "rigid_trunk": self.rigid_trunk,
                "default_trunk_offset": self.default_trunk_offset,
                "default_arm_pose": self.default_arm_pose,
            }
        )
        return dump


class VirtualPlanarJoint(object):
    """A wrapper for a planar (2DOF translation and 1DOF rotation) virtual joint between two robot body parts.

    This wrapper generates the 6 separate VirtualJoint instances needed for such a mechanism, and accumulates their
    set_pos calls to provide a single callback. Note that only the 3 actuated joints must be set for this
    wrapper to trigger its callback.
    """

    COMPONENT_SUFFIXES = ["x", "y", "z", "rx", "ry", "rz"]
    ACTUATED_COMPONENT_SUFFIXES = ["x", "y", "rz"]  # 0, 1, 5

    def __init__(
        self,
        joint_name,
        parent_link,
        child_link,
        command_callback,
        reset_callback,
        lower_limits=None,
        upper_limits=None,
    ):
        self.joint_name = joint_name
        self.parent_link = parent_link
        self.child_link = child_link
        self._command_callback = command_callback
        self._reset_callback = reset_callback

        self._joints = [
            VirtualJoint(
                joint_name="%s_%s" % (self.joint_name, name),
                joint_type=p.JOINT_PRISMATIC if i < 3 else p.JOINT_REVOLUTE,
                get_state_callback=lambda dof=i: (self.get_state()[dof], None, None),
                set_pos_callback=lambda pos, dof=i: self.set_pos(dof, pos),
                reset_pos_callback=lambda pos, dof=i: self.reset_pos(dof, pos),
                lower_limit=lower_limits[i] if lower_limits is not None else None,
                upper_limit=upper_limits[i] if upper_limits is not None else None,
            )
            for i, name in enumerate(VirtualPlanarJoint.COMPONENT_SUFFIXES)
        ]

        self._get_actuated_indices = lambda lst: [lst[0], lst[1], lst[5]]

        self._reset_stored_control()
        self._reset_stored_reset()

    def get_state(self):
        pos, orn = self.child_link.get_position_orientation()

        if self.parent_link is not None:
            pos, orn = p.multiplyTransforms(*p.invertTransform(*self.parent_link.get_position_orientation()), pos, orn)

        # Stack the position and the Euler orientation
        return list(pos) + list(p.getEulerFromQuaternion(orn))

    def get_joints(self):
        """Gets the 1DOF VirtualJoints belonging to this 6DOF joint."""
        return tuple(self._joints)

    def set_pos(self, dof, val):
        """Calls the command callback with values for all 3 actuated DOF once the setter has been called for each of them."""
        self._stored_control[dof] = val

        if all(ctrl is not None for ctrl in self._get_actuated_indices(self._stored_control)):
            self._command_callback(self._stored_control)
            self._reset_stored_control()

    def reset_pos(self, dof, val):
        """Calls the reset callback with values for all 3 actuated DOF once the setter has been called for each of them."""
        self._stored_reset[dof] = val

        if all(reset_val is not None for reset_val in self._get_actuated_indices(self._stored_reset)):
            self._reset_callback(self._stored_reset)
            self._reset_stored_reset()

    def _reset_stored_control(self):
        self._stored_control = [None] * len(self._joints)

    def _reset_stored_reset(self):
        self._stored_reset = [None] * len(self._joints)