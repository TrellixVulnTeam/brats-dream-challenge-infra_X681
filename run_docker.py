"""Run training synthetic docker models"""
from __future__ import print_function
import argparse
import getpass
import os
import tarfile
import time
import glob

import docker
import synapseclient


def create_log_file(log_filename, log_text=None):
    """Create log file"""
    with open(log_filename, 'w') as log_file:
        if log_text is not None:
            if isinstance(log_text, bytes):
                log_text = log_text.decode("utf-8")
            log_file.write(log_text.encode("ascii", "ignore").decode("ascii"))
        else:
            log_file.write("No Logs")


def store_log_file(syn, log_filename, parentid, store=True):
    """Store log file"""
    statinfo = os.stat(log_filename)
    if statinfo.st_size > 0 and statinfo.st_size/1000.0 <= 50:
        ent = synapseclient.File(log_filename, parent=parentid)
        if not store:
            try:
                syn.store(ent)
            except synapseclient.core.exceptions.SynapseHTTPError as err:
                print(err)


def remove_docker_container(container_name):
    """Remove docker container"""
    client = docker.from_env()
    try:
        cont = client.containers.get(container_name)
        cont.stop()
        cont.remove()
    except Exception:
        print("Unable to remove container")


def remove_docker_image(image_name):
    """Remove docker image"""
    client = docker.from_env()
    try:
        client.images.remove(image_name, force=True)
    except Exception:
        print("Unable to remove image")


def tar(directory, tar_filename):
    """Tar all files in a directory

    Args:
        directory: Directory path to files to tar
        tar_filename:  tar file path
    """
    with tarfile.open(tar_filename, "w") as tar_o:
        tar_o.add(directory)


def untar(directory, tar_filename):
    """Untar a tar file into a directory

    Args:
        directory: Path to directory to untar files
        tar_filename:  tar file path
    """
    with tarfile.open(tar_filename, "r") as tar_o:
        tar_o.extractall(path=directory)


def main(syn, args):
    """Run docker model"""
    if args.status == "INVALID":
        raise Exception("Docker image is invalid")

    # The new toil version doesn't seem to pull the docker config file from
    # .docker/config.json...
    # client = docker.from_env()
    client = docker.DockerClient(base_url='unix://var/run/docker.sock')
    config = synapseclient.Synapse().getConfigFile(
        configPath=args.synapse_config
    )
    authen = dict(config.items("authentication"))
    client.login(username=authen['username'],
                 password=authen['password'],
                 registry="https://docker.synapse.org")

    print(getpass.getuser())

    # Get Docker image to run and volumes to be mounted.
    docker_image = args.docker_repository + "@" + args.docker_digest
    output_dir = os.getcwd()

    # For the input directory, there will be a different case folder per
    # Docker run, e.g. /path/to/BraTS2021_00001, /path/to/BraTS2021_00013,
    # etc. In total, there will be 5 Docker runs for the validation data,
    # 500 for the testing data.
    for root, sub_dirs, _ in os.walk(args.input_dir):
        for sub_dir in sub_dirs:
            case_folder = os.path.join(root, sub_dir)
            case_id = case_folder[-5:]

            print("mounting volumes")

            # Specify the input directory with 'ro' permissions, output with
            # 'rw' permissions.
            # mounted_volumes = {output_dir: '/output:rw',
            #                    case_folder: '/input:ro'}

            # FOR NOW: stress-test with these mounted directories, which are
            #          expected in last year's models.
            mounted_volumes = {
                output_dir: '/data/results:rw',
                case_folder: '/data:rw'
            }

            # Format the mounted volumes so that Docker SDK can understand.
            all_volumes = [output_dir, case_folder]
            volumes = {}
            for vol in all_volumes:
                volumes[vol] = {'bind': mounted_volumes[vol].split(":")[0],
                                'mode': mounted_volumes[vol].split(":")[1]}

            # Run the Docker container in detached mode and with access
            # to the GPU, also making note of the start time.
            print("running container")
            start_time = time.time()
            time_elapsed = 0
            try:
                container_name = f"{args.subissionid}_case{case_id}"
                container = client.containers.run(docker_image,
                                                  detach=True,
                                                  volumes=volumes,
                                                  name=container_name,
                                                  network_disabled=True,
                                                  stderr=True,
                                                  runtime="nvidia")
            except docker.errors.APIError as err:
                remove_docker_container(container_name)
                print(str(err))
                errors = str(err) + "\n"

            # Create a logfile to catch stdout/stderr from the Docker run.
            print("creating logfile")
            log_filename = args.submissionid + "_log.txt"
            open(log_filename, 'w').close()

            # If container is running, monitor the time elapsed -- if it
            # exceeds the runtime quota, stop the container. Logs should
            # also be captured every 60 seconds.  Remove the container
            if container is not None:
                while container in client.containers.list():
                    time_elapsed = time.time() - start_time
                    if time_elapsed > args.runtime_quota:
                        container.stop()
                        break

                    log_text = container.logs()
                    create_log_file(log_filename, log_text=log_text)
                    store_log_file(syn, log_filename,
                                   args.parentid, store=args.store)
                    time.sleep(60)

                # Note the reason for container exit in the logs if time
                # limit is reached.
                if time_elapsed > args.runtime_quota:
                    log_text = (f"Time limit of {args.runtime_quota}s reached"
                                f"for case {case_id} - no output generated.")

                else:
                    # Must run again to make sure all the logs are captured
                    log_text = container.logs()

                create_log_file(log_filename, log_text=log_text)
                store_log_file(syn, log_filename,
                               args.parentid, store=args.store)
                container.remove()

            statinfo = os.stat(log_filename)
            if statinfo.st_size == 0:
                create_log_file(log_filename, log_text=errors)
                store_log_file(syn, log_filename,
                               args.parentid, store=args.store)
    print("finished inference")
    remove_docker_image(docker_image)

    # Check for prediction files once the Docker run is complete. Tar
    # the predictions if found; else, mark the submission as INVALID.
    if glob.glob("*.nii.gz"):
        os.mkdir("predictions")
        for nifti in glob.glob("*.nii.gz"):
            os.rename(nifti, os.path.join("predictions", nifti))
        tar("predictions", "predictions.tar.gz")
    else:
        raise Exception(
            "No *.nii.gz files found; please check whether running the "
            "Docker container locally will result in a NIfTI file."
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--submissionid", required=True,
                        help="Submission Id")
    parser.add_argument("-p", "--docker_repository", required=True,
                        help="Docker Repository")
    parser.add_argument("-d", "--docker_digest", required=True,
                        help="Docker Digest")
    parser.add_argument("-i", "--input_dir", required=True,
                        help="Input Directory")
    parser.add_argument("-c", "--synapse_config", required=True,
                        help="credentials file")
    parser.add_argument("-rt", "--runtime_quota",
                        help="runtime quota in seconds")
    parser.add_argument("--store", action='store_true',
                        help="to store logs")
    parser.add_argument("--parentid", required=True,
                        help="Parent Id of submitter directory")
    parser.add_argument("--status", required=True, help="Docker image status")
    args = parser.parse_args()
    syn = synapseclient.Synapse(configPath=args.synapse_config)
    syn.login()
    main(syn, args)
