""" Demo showing off text rendering in iGibson - in both VR and non-VR modes
"""
import numpy as np
import os
import pybullet as p
import pybullet_data
import time

import gibson2
from gibson2.render.mesh_renderer.mesh_renderer_cpu import MeshRendererSettings
from gibson2.render.mesh_renderer.mesh_renderer_vr import VrSettings
from gibson2.scenes.igibson_indoor_scene import InteractiveIndoorScene
from gibson2.objects.articulated_object import ArticulatedObject
from gibson2.objects.vr_objects import VrAgent
from gibson2.objects.ycb_object import YCBObject
from gibson2.simulator import Simulator
from gibson2 import assets_path

# Objects in the benchmark - corresponds to Rs kitchen environment, for range of items and
# transferability to the real world
# Note: the scene will automatically load in walls/ceilings/floors in addition to these objects
benchmark_names = [
    'door_54',
    'trash_can_25',
    'counter_26',
    'bottom_cabinet_39',
    'fridge_40',
    'bottom_cabinet_41',
    'sink_42',
    'microwave_43',
    'dishwasher_44',
    'oven_45',
    'bottom_cabinet_46',
    'top_cabinet_47',
    'top_cabinet_48',
    'top_cabinet_49',
    'top_cabinet_50',
    'top_cabinet_51'
]

# Whether to use VR or not
VR_MODE = False
# Set to true to print Simulator step() statistics
PRINT_STATS = True
# Set to true to use gripper instead of VR hands
USE_GRIPPER = False

# HDR files for PBR rendering
hdr_texture = os.path.join(
    gibson2.ig_dataset_path, 'scenes', 'background', 'probe_02.hdr')
hdr_texture2 = os.path.join(
    gibson2.ig_dataset_path, 'scenes', 'background', 'probe_03.hdr')
light_modulation_map_filename = os.path.join(
    gibson2.ig_dataset_path, 'scenes', 'Rs_int', 'layout', 'floor_lighttype_0.png')
background_texture = os.path.join(
    gibson2.ig_dataset_path, 'scenes', 'background', 'urban_street_01.jpg')

# VR rendering settings
vr_rendering_settings = MeshRendererSettings(optimized=True,
                                            fullscreen=False,
                                            env_texture_filename=hdr_texture,
                                            env_texture_filename2=hdr_texture2,
                                            env_texture_filename3=background_texture,
                                            light_modulation_map_filename=light_modulation_map_filename,
                                            enable_shadow=True, 
                                            enable_pbr=True,
                                            msaa=True,
                                            light_dimming_factor=1.0)

vr_settings = VrSettings(use_vr=VR_MODE)
s = Simulator(mode='vr', 
            use_fixed_fps = True,
            rendering_settings=vr_rendering_settings, 
            vr_settings=vr_settings)

scene = InteractiveIndoorScene('Rs_int')
scene._set_obj_names_to_load(benchmark_names)
s.import_ig_scene(scene)
p.setAdditionalSearchPath(pybullet_data.getDataPath())

if not VR_MODE:
    camera_pose = np.array([0, -1, 1.2])
    view_direction = np.array([0, 1, 0])
    s.renderer.set_camera(camera_pose, camera_pose + view_direction, [0, 0, 1])
    s.renderer.set_fov(90)

if VR_MODE:
    vr_agent = VrAgent(s, use_gripper=USE_GRIPPER)
    # Move VR agent to the middle of the kitchen
    s.set_vr_start_pos(start_pos=[0,2.1,0], vr_height_offset=-0.02)

# Mass values to use for each object type - len(masses) objects will be created of each type
masses = [1, 5, 10]

# List of objects to load with name: filename, type, scale, base orientation, start position, spacing vector and spacing value
obj_to_load = {
    'mustard': ('006_mustard_bottle', 'ycb', 1, (0.0, 0.0, 0.0, 1.0), (0.0, 1.6, 1.18), (-1, 0, 0), 0.15),
    'marker': ('040_large_marker', 'ycb', 1, (0.0, 0.0, 0.0, 1.0), (1.5, 2.6, 0.92), (0, -1, 0), 0.15),
    'can': ('005_tomato_soup_can', 'ycb', 1, (0.0, 0.0, 0.0, 1.0), (1.7, 2.6, 0.95), (0, -1, 0), 0.15),
    'drill': ('035_power_drill', 'ycb', 1, (0.0, 0.0, 0.0, 1.0), (1.5, 2.2, 1.15), (0, -1, 0), 0.2),
    'small_jenga': ('jenga/jenga.urdf', 'pb', 1, (0.000000, 0.707107, 0.000000, 0.707107), (-0.9, 1.6, 1.18), (-1, 0, 0), 0.1),
    'large_jenga': ('jenga/jenga.urdf', 'pb', 2, (0.000000, 0.707107, 0.000000, 0.707107), (-1.3, 1.6, 1.31), (-1, 0, 0), 0.15),
    'small_duck': ('duck_vhacd.urdf', 'pb', 1, (0.000000, 0.000000, 0.707107, 0.707107), (-1.8, 1.95, 1.12), (1, 0, 0), 0.15),
    'large_duck': ('duck_vhacd.urdf', 'pb', 2, (0.000000, 0.000000, 0.707107, 0.707107), (-1.95, 2.2, 1.2), (1, 0, 0), 0.2),
    'small_sphere': ('sphere_small.urdf', 'pb', 1, (0.000000, 0.000000, 0.707107, 0.707107), (-0.5, 1.63, 1.15), (-1, 0, 0), 0.15),
    'large_sphere': ('sphere_small.urdf', 'pb', 2, (0.000000, 0.000000, 0.707107, 0.707107), (-0.5, 1.47, 1.15), (-1, 0, 0), 0.15)
}

for name in obj_to_load:
    fpath, obj_type, scale, orn, pos, space_vec, space_val = obj_to_load[name]
    for i in range(len(masses)):
        if obj_type == 'ycb':
            handle = YCBObject(fpath, scale=scale)
        elif obj_type == 'pb':
            handle = ArticulatedObject(fpath, scale=scale)
        
        s.import_object(handle, use_pbr=False, use_pbr_mapping=False)
        # Calculate new position along spacing vector
        new_pos = (pos[0] + space_vec[0] * space_val * i, pos[1] + space_vec[1] * space_val * i, pos[2] + space_vec[2] * space_val * i)
        handle.set_position(new_pos)
        handle.set_orientation(orn)
        p.changeDynamics(handle.body_id, -1, mass=masses[i])

# Generate text
text_1_params = ([500, 800], 1.5, [0, 0, 1])
text_2_params = ([500, 500], 2.0, [1, 0, 0])
text_3_params = ([300, 300], 1.5, [0, 1, 0])

if not VR_MODE:
    text_1 = s.add_normal_text(text_data='iGibson Text!', pos=text_1_params[0], scale=text_1_params[1], color=text_1_params[2], background_color=[1,1,1,0.8])
    text_2 = s.add_normal_text(text_data='IS AWESOME!', pos=text_2_params[0], scale=text_2_params[1], color=text_2_params[2], background_color=[1,1,1,0.5])
    text_3 = s.add_normal_text(text_data='good job\ngood multiline\ntest line!', pos=text_3_params[0], scale=text_3_params[1], color=text_3_params[2], background_color=[0,0,0,1.0])
else:
    text_1 = s.add_vr_overlay_text(text_data='iGibson Text!', pos=text_1_params[0], scale=text_1_params[1], color=text_1_params[2])
    text_2 = s.add_vr_overlay_text(text_data='IS AWESOME!', pos=text_2_params[0], scale=text_2_params[1], color=text_2_params[2])
    text_3 = s.add_vr_overlay_text(text_data='HeLlo FrIeNds!!!', pos=text_3_params[0], scale=text_3_params[1], color=text_3_params[2])

    # TODO: Remove this later!
    #test_img_path = os.path.join(assets_path, 'test', 'test_overlay.jpg')

# Main simulation loop
while True:
    s.step(print_stats=PRINT_STATS)

    # Animate text across the screen
    t1 = float(np.sin((2 * np.pi/5) * time.time()))
    t2 = float(np.cos((2 * np.pi/8) * time.time()))
    text_1_new_pos = [text_1_params[0][0] + 400 * t1, text_1_params[0][1]]
    text_2_new_pos = [text_2_params[0][0], text_2_params[0][1] + 300 * t2]
    text_1.set_attribs(pos=text_1_new_pos, color=[0, 0, float(np.abs(t1))])
    text_2.set_attribs(pos=text_2_new_pos, color=[float(np.abs(t2)), 0, 0])

    if VR_MODE:
        vr_agent.update()

    if not VR_MODE:
        t3 = float(np.cos((2 * np.pi/50) * time.time()))
        view_direction = np.array([0 + t3, 1, 0])
        s.renderer.set_camera(camera_pose, camera_pose + view_direction, [0, 0, 1])

s.disconnect()