import logging

import numpy as np
import pybullet as p

from igibson.tasks.point_nav_random_task import PointNavRandomTask
from igibson.objects.visual_marker import VisualMarker
from igibson.utils.utils import cartesian_to_polar, restoreState, l2_distance, rotate_vector_3d

class AudioGoalNavTask(PointNavRandomTask):
    """
    Redefine the task (reward functions)
    """
    def __init__(self, env):
        super(AudioGoalNavTask, self).__init__(env)
        self.target_obj  = None
        self.load_target(env)

    def reset_agent(self, env):
        super().reset_agent(env)
        self.target_obj.set_position(self.target_pos)
        audio_obj_id = self.target_obj.get_body_ids()[0]
        env.audio_system.registerSource(audio_obj_id, self.config['audio_dir'], enabled=True)
        env.audio_system.setSourceRepeat(audio_obj_id)

    def load_target(self, env):
        """
        Load target marker, hidden by default
        :param env: environment instance
        """

        cyl_length = 0.2

        self.target_obj = VisualMarker(
            visual_shape=p.GEOM_CYLINDER,
            rgba_color=[0, 0, 1, 0.3],
            radius=self.dist_tol,
            length=cyl_length,
            initial_offset=[0, 0, cyl_length / 2.0],
        )

        env.simulator.import_object(self.target_obj)

        # The visual object indicating the target location may be visible
        for instance in self.target_obj.renderer_instances:
            instance.hidden = not self.visible_target

    def get_task_obs(self, env):
        """
        Get current velocities

        :param env: environment instance
        :return: task-specific observation
        """
        # linear velocity along the x-axis
        linear_velocity = rotate_vector_3d(env.robots[0].get_linear_velocity(), *env.robots[0].get_rpy())[0]
        # angular velocity along the z-axis
        angular_velocity = rotate_vector_3d(env.robots[0].get_angular_velocity(), *env.robots[0].get_rpy())[2]
        task_obs = np.append(linear_velocity, angular_velocity)

        return task_obs

class AudioGoalVRNavTask(PointNavRandomTask):
    """
    Redefine the task (reward functions)
    """
    def __init__(self, env):
        super(AudioGoalVRNavTask, self).__init__(env)

    def reset_agent(self, env):
        super().reset_agent(env)

    def get_task_obs(self, env):
        """
        Get current velocities

        :param env: environment instance
        :return: task-specific observation
        """
        # linear velocity along the x-axis
        linear_velocity = rotate_vector_3d(env.robots[0].get_linear_velocity(), *env.robots[0].get_rpy())[0]
        # angular velocity along the z-axis
        angular_velocity = rotate_vector_3d(env.robots[0].get_angular_velocity(), *env.robots[0].get_rpy())[2]
        task_obs = np.append(linear_velocity, angular_velocity)

        return task_obs

class AudioPointGoalNavTask(AudioGoalNavTask):
    """ AudioGoal, but with target position information in observation space """
    def __init__(self, env):
        super().__init__(env)

    def get_task_obs(self, env):
        """
        Get task-specific observation, including goal position, current velocities, etc.

        :param env: environment instance
        :return: task-specific observation
        """
        task_obs = self.global_to_local(env, self.target_pos)[:2]
        if self.goal_format == "polar":
            task_obs = np.array(cartesian_to_polar(task_obs[0], task_obs[1]))

        # linear velocity along the x-axis
        linear_velocity = rotate_vector_3d(env.robots[0].get_linear_velocity(), *env.robots[0].get_rpy())[0]
        # angular velocity along the z-axis
        angular_velocity = rotate_vector_3d(env.robots[0].get_angular_velocity(), *env.robots[0].get_rpy())[2]
        task_obs = np.append(task_obs, [linear_velocity, angular_velocity])

        return task_obs