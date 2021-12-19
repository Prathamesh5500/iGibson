import itertools
import json
import os
import xml.etree.ElementTree as ET

import numpy as np
import pybullet as p
import tqdm
import trimesh

import igibson

SKIP_EXISTING = True
IGNORE_ERRORS = False


def get_categories():
    dir = os.path.join(igibson.ig_dataset_path, "objects")
    return [cat for cat in os.listdir(dir) if os.path.isdir(get_category_directory(cat))]


def get_category_directory(category):
    return os.path.join(igibson.ig_dataset_path, "objects", category)


def get_urdf_filename(folder):
    return os.path.join(folder, os.path.basename(folder) + ".urdf")


def get_metadata_filename(objdir):
    return os.path.join(objdir, "misc", "metadata.json")


def validate_meta(meta):
    if "link_bounding_boxes" not in meta:
        return "No bounding box found"

    lbbs = meta["link_bounding_boxes"]
    for link, lbb in lbbs.items():
        for mesh_type in ["visual", "collision"]:
            if mesh_type not in lbb:
                return "No " + mesh_type + " bounding box found for link " + link

            lbb_thistype = lbb[mesh_type]
            if lbb_thistype is None:
                continue

            for bb_type in ["axis_aligned", "oriented"]:
                if bb_type not in lbb_thistype:
                    return "No " + bb_type + "bounding box found for link " + link + ", type " + mesh_type

    return None


def main():
    # Collect the relevant categories.
    categories = get_categories()
    print("%d categories: %s" % (len(categories), ", ".join(categories)))

    # Now collect the actual objects.
    objects = []
    for cat in categories:
        cd = get_category_directory(cat)
        for objdir in os.listdir(cd):
            objdirfull = os.path.join(cd, objdir)
            objects.append(objdirfull)

    objects.sort()
    print("%d objects.\n" % len(objects))

    # Filter out already-processed objects.
    objects_to_process = []
    for objdirfull in objects:
        mfn = get_metadata_filename(objdirfull)
        with open(mfn, "r") as mf:
            meta = json.load(mf)

        if SKIP_EXISTING:
            validation_error = validate_meta(meta)
            if validation_error is None:
                continue
            else:
                print("%s: %s" % (objdirfull, validation_error))

        objects_to_process.append(objdirfull)

    # Now process the remaining.
    for objdirfull in tqdm.tqdm(objects_to_process):
        mfn = get_metadata_filename(objdirfull)
        with open(mfn, "r") as mf:
            meta = json.load(mf)

        try:
            urdf_file = get_urdf_filename(objdirfull)
            tree = ET.parse(urdf_file)
            links = tree.findall("link")

            link_bounding_boxes = {}
            for link in links:
                this_link_bounding_boxes = {}

                for mesh_type in ["collision", "visual"]:
                    try:
                        # For each type of mesh, we first combine all of the different mesh files into a single mesh.
                        mesh_type_nodes = link.findall(mesh_type)
                        combined_mesh = trimesh.Trimesh()

                        if not mesh_type_nodes:
                            # None here means that the object does not have this type of mesh.
                            this_link_bounding_boxes[mesh_type] = None
                            continue

                        for mesh_type_node in mesh_type_nodes:
                            origin = mesh_type_node.find("origin")
                            rotation = origin.attrib["rpy"] if "rpy" in origin.attrib else "0 0 0"
                            rotation = np.array([float(x) for x in rotation.split(" ")])
                            assert rotation.shape == (3,)
                            translation = origin.attrib["xyz"] if "xyz" in origin.attrib else "0 0 0"
                            translation = np.array([float(x) for x in translation.split(" ")])
                            assert translation.shape == (3,)
                            mesh_nodes = mesh_type_node.findall("geometry/mesh")

                            for mesh_node in mesh_nodes:
                                object_file = os.path.join(objdirfull, mesh_node.attrib["filename"])
                                mesh = trimesh.load(object_file, force="mesh", skip_materials=True)

                                scale = mesh_node.attrib["scale"] if "scale" in mesh_node.attrib else "1 1 1"
                                scale = np.array([float(x) for x in scale.split(" ")])

                                # Apply the translation & scaling and add the mesh onto the combined mesh.
                                combined_mesh += mesh.apply_transform(
                                    trimesh.transformations.compose_matrix(
                                        scale=scale, angles=rotation, translate=translation
                                    )
                                )

                        # Now that we have the combined mesh, let's simply compute the bounding box.
                        bbox = combined_mesh.bounding_box
                        bbox = {
                            "extent": np.array(bbox.primitive.extents).tolist(),
                            "transform": np.array(bbox.primitive.transform).tolist(),
                        }
                        bbox_oriented = combined_mesh.bounding_box_oriented
                        bbox_oriented = {
                            "extent": np.array(bbox_oriented.primitive.extents).tolist(),
                            "transform": np.array(bbox_oriented.primitive.transform).tolist(),
                        }

                        oriented_bbox = combined_mesh.bounding_box_oriented
                        oriented_bbox_dict = {
                            "extent": np.array(oriented_bbox.primitive.extents).tolist(),
                            "transform": np.array(oriented_bbox.primitive.transform).tolist(),
                        }

                        this_link_bounding_boxes[mesh_type] = {"axis_aligned": bbox, "oriented": bbox_oriented}
                    except:
                        print(
                            "Problem with %s mesh in link %s in obj %s" % (mesh_type, link.attrib["name"], objdirfull)
                        )

                        # We are quite sensitive against missing collision meshes!
                        if mesh_type == "collision":
                            raise

                if len(this_link_bounding_boxes) > 0:
                    link_bounding_boxes[link.attrib["name"]] = this_link_bounding_boxes

            if len(link_bounding_boxes) == 0:
                raise ValueError("No links have a mesh for object %s" % objdirfull)

            meta["link_bounding_boxes"] = link_bounding_boxes

            with open(mfn, "w") as mf:
                json.dump(meta, mf)
        except:
            combined_mesh.show()

            if IGNORE_ERRORS:
                print("Something went wrong with ", objdirfull)
            else:
                raise


if __name__ == "__main__":
    main()
