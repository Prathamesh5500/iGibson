import cv2
import matplotlib.pyplot as plt
from scipy.spatial import distance
from scipy.interpolate import UnivariateSpline

from PIL import Image

import numpy as np
from igibson.scenes.igibson_indoor_scene import InteractiveIndoorScene
from igibson.simulator import Simulator

IMAGE_HEIGHT = 720
IMAGE_WIDTH = 1024
MAX_NUM_FRAMES = 10000
FRAME_BATCH_SIZE = 5

frame_size = (1024, 720)
fourcc = cv2.VideoWriter_fourcc(*"MP4V")
output_video = cv2.VideoWriter(
    "output_video_Rs_int.mp4", fourcc, 20.0, frame_size)


class GenerateDataset(object):
    def __init__(self):
        self.sim = Simulator(
            image_height=IMAGE_HEIGHT,
            image_width=IMAGE_WIDTH,
            mode='headless',
        )

        scene = InteractiveIndoorScene(
            scene_id="Ihlen_1_int", not_load_object_categories=["door"], trav_map_type="no_door", trav_map_erosion=5, trav_map_resolution=0.1)
        self.sim.import_scene(scene)
        self.floor = self.sim.scene.get_random_floor()
        self.check_points = []

        self.camera_angle = 0.0
        self.camera_angular_velocity = 0.0
        self.camera_angle_kp = 1e-1
        self.camera_angle_kd = 1

        for room_instance in self.sim.scene.room_ins_name_to_ins_id:
            lower, upper = self.sim.scene.get_aabb_by_room_instance(
                room_instance)  # Axis Aligned Bounding Box
            x_cord, y_cord, _ = (upper - lower)/2 + lower
            self.check_points.append((x_cord, y_cord))

    def prepare_spline_functions(self, shortest_path):
        self.spline_functions = []
        path_length = len(shortest_path)
        self.spline_functions.append(UnivariateSpline(
            range(path_length), shortest_path[:, 0], s=0.7, k=3))
        self.spline_functions.append(UnivariateSpline(
            range(path_length), shortest_path[:, 1], s=0.7, k=3))

    def get_interpolated_steps(self, step):
        curr_x, curr_y = self.spline_functions[0](
            step), self.spline_functions[1](step)

        next_x, next_y = self.spline_functions[0](
            step + 1), self.spline_functions[1](step + 1)

        dist_to_next_step = distance.euclidean(
            (curr_x, curr_y), (next_x, next_y))

        path_length = int(100 * dist_to_next_step)
        interpolated_points = []
        for i in range(path_length):
            curr_step = step + (1.0/path_length*i)
            interpolated_points.append([self.spline_functions[0](
                curr_step), self.spline_functions[1](curr_step)])
        return np.array(interpolated_points)

    def render_image(self, step, next_step):
        x, y, z = step[0], step[1], 1
        tar_x, tar_y, tar_z = next_step[0], next_step[1], 1

        self.sim.renderer.set_camera(
            [x, y, z], [tar_x, tar_y, tar_z], [0, 0, 1])
        frames = self.sim.renderer.render(modes=("rgb", "3d"))

        # Render 3d points as depth map
        depth = np.linalg.norm(frames[1][:, :, :3], axis=2)
        depth /= depth.max()
        frames[1][:, :, :3] = depth[..., None]

        self.sim.step()
        img = np.array(Image.fromarray((255 * frames[0]).astype(np.uint8)))
        cv2.imshow("test", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        output_video.write(cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        cv2.waitKey(1)

    def generate(self):
        # source, target, camera_up
        check_points = self.check_points
        shortest_path = []

        for i in range(len(check_points)):
            steps = self.sim.scene.get_shortest_path(
                self.floor, check_points[i-1][:2], check_points[i][:2], True)[0]
            for j in range(len(steps)-1):
                step = steps[j]
                shortest_path.append(step)
        shortest_path = np.array(shortest_path)
        self.prepare_spline_functions(shortest_path)

        steps = []
        for i in range(len(shortest_path)):
            for step in self.get_interpolated_steps(i):
                step = np.append(step, np.arctan2(step[1], step[0]))
                steps.append(step)
        steps = np.array(steps)

        for i in range(len(steps)-11):
            # TODO: check when there is a new trajectory
            curr_step = steps[i]
            next_step = np.average(steps[i+1:i+10], axis=0)
            sharp_turn_heuristic = np.linalg.norm(curr_step - next_step)
            if sharp_turn_heuristic <= 0.008:
                print("Sharp Turn Encountered")
            self.render_image(curr_step, next_step)

    def disconnect_simulator(self):
        self.sim.disconnect()


dataset_generator = GenerateDataset()
dataset_generator.generate()
dataset_generator.disconnect_simulator()
output_video.release()
