import logging
import os
import time
import numpy as np
import igibson
from random import random
from igibson import object_states
from igibson.objects.articulated_object import ArticulatedObject, URDFObject
from igibson.objects.multi_object_wrappers import ObjectGrouper, ObjectMultiplexer
from igibson.utils.assets_utils import get_ig_model_path


# Hyper parameters
num_trials = {
    "training": 5,
    "collecting": 5
}
default_robot_pose = ([-0.3, 0, 1], [0, 0, 0, 1])
timeout = 60
intro_paragraph = """Welcome to the slice experiment! 
There will be a knife and a mushroom on the table. 
--------------------------------
1. Grab the knife with your hand
2. Slice the mushroom into 2 pieces with it.
3. Try to use your dominant hand when slicing.
4. Move your hand away from the table when restarting.
--------------------------------
Go to the starting point (red marker) and face the desk
Press menu button on the right controller to begin.
"""


def import_obj(s):
    table = ArticulatedObject("igibson/examples/vr/visual_disease_demo_mtls/table/table.urdf", scale=1, rendering_params={"use_pbr": False, "use_pbr_mapping": False})
    s.import_object(table)
    table.set_position_orientation((0.500000, 0.000000, 0.000000), (0.000000, 0.000000, 0.707107, 0.707107))
    
    # slice-related objects
    slicer = URDFObject(f"{igibson.ig_dataset_path}/objects/carving_knife/14_1/14_1.urdf", name="knife", abilities={"slicer": {}})
    s.import_object(slicer)

    obj_part_list = []
    simulator_obj = URDFObject(
        f"{igibson.ig_dataset_path}/objects/mushroom/41_1/41_1.urdf", 
        name="slicable_object", 
        category="mushroom",
        scale=np.ones(3) * 0.6,
    )
    whole_object = simulator_obj
    obj_part_list.append(simulator_obj)
    object_parts = []
    for i, part in enumerate(simulator_obj.metadata["object_parts"]):
        category = part["category"]
        model = part["model"]
        # Scale the offset accordingly
        part_pos = part["pos"] * whole_object.scale
        part_orn = part["orn"]
        model_path = get_ig_model_path(category, model)
        filename = os.path.join(model_path, model + ".urdf")
        obj_name = whole_object.name + "_part_{}".format(i)
        simulator_obj_part = URDFObject(
            filename,
            name=obj_name,
            category=category,
            model_path=model_path,
            scale=whole_object.scale,
        )
        obj_part_list.append(simulator_obj_part)
        object_parts.append((simulator_obj_part, (part_pos, part_orn)))
    grouped_obj_parts = ObjectGrouper(object_parts)
    slicable = ObjectMultiplexer(whole_object.name + "_multiplexer", [whole_object, grouped_obj_parts], 0)
    s.import_object(slicable)
        
    

    ret = {}
    ret["slicer"] = slicer
    ret["slicable"] = slicable
    ret["obj_part_list"] = obj_part_list
    ret["slicer_initial_extended_state"] = slicer.dump_state()
    ret["slicable_initial_extended_state"] = slicable.dump_state()
    return ret

def set_obj_pos(objs):
    # restore object state
    objs["slicer"].load_state(objs["slicer_initial_extended_state"])
    objs["slicer"].force_wakeup()
    objs["slicable"].load_state(objs["slicable_initial_extended_state"])
    objs["slicable"].force_wakeup()
    objs["slicer"].set_position_orientation((0.300000, 0.000000, 1.1), ( 0.707107, 0.000000, 0.707107, 0.000000))
    # Set these objects to be far-away locations
    for i, new_urdf_obj in enumerate(objs["obj_part_list"]):
        new_urdf_obj.set_position([100 + i, 100, -100])
        new_urdf_obj.force_wakeup()
    objs["slicable"].set_position((0.300000, 0.30000, 1.1))
    objs["slicable"].force_wakeup()


def main(s, log_writer, disable_save, debug, robot, objs, args):
    is_valid, success = True, False
    success_time = 0
    start_time = time.time()
    while True:
        robot.apply_action(s.gen_vr_robot_action())
        s.step(print_stats=debug)
        if log_writer and not disable_save:
            log_writer.process_frame()     
        s.update_vi_effect(debug)


        if objs["slicable"].states[object_states.Sliced].get_value():
            if success_time:
                if time.time() - success_time > 0.5:
                    success = True
                    break
            else:
                success_time = time.time()
        else:
            success_time = 0
        # End demo by pressing overlay toggle
        if s.query_vr_event("left_controller", "overlay_toggle"):
            is_valid = False
            break
        if s.query_vr_event("right_controller", "overlay_toggle"):
            break

        # timeout
        if time.time() - start_time > timeout:
            break
    return is_valid, success


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
