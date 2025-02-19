import argparse
import logging
import os
from typing import Dict

import numpy as np
import pandas as pd
import distutils.util
from envyaml import EnvYAML
from evo.core.trajectory import PoseTrajectory3D


class ArgparseKeyValueAction(argparse.Action):
    # Constructor calling
    def __call__(self, parser, namespace,
                 values, option_string=None):
        setattr(namespace, self.dest, dict())

        for value in values:
            key, value = value.split('=')
            value = str_to_likely_type(value)
            getattr(namespace, self.dest)[key] = value


def str_to_likely_type(value: str):
    try:
        value = int(value)
    except ValueError:
        try:
            value = float(value)
        except ValueError:
            try:
                value = distutils.util.strtobool(value) == 1
            except ValueError:
                pass
                # try to parse array
                if value.startswith('[') and value.endswith(']'):
                    try:
                        value = eval(value)
                    except:
                        pass
    return value


def convert_to_evo_trajectory(df_poses, prefix="", filter_invalid_entries=True) -> (PoseTrajectory3D, np.ndarray):
    t_xyz_wxyz = df_poses[['t', prefix + 'p_x', prefix + 'p_y', prefix + 'p_z',
                           prefix + 'q_w', prefix + 'q_x', prefix + 'q_y', prefix + 'q_z']].to_numpy()

    return convert_t_xyz_wxyz_to_evo_trajectory(t_xyz_wxyz, filter_invalid_entries), t_xyz_wxyz


def convert_t_xyz_wxyz_to_evo_trajectory(t_xyz_wxyz, filter_invalid_entries=True):
    nan_percentage, traj_has_nans = get_nans_in_trajectory(t_xyz_wxyz)
    if nan_percentage > 0:
        print(F"WARNING: {nan_percentage:.1f}% NaNs found in trajectory estimate")
    t_is_minus_one = t_xyz_wxyz[:, 0] == -1
    invalid_data_mask = traj_has_nans | t_is_minus_one
    if filter_invalid_entries:
        t_xyz_wxyz = t_xyz_wxyz[~invalid_data_mask, :]
    return PoseTrajectory3D(t_xyz_wxyz[:, 1:4], t_xyz_wxyz[:, 4:8], t_xyz_wxyz[:, 0])


def get_nans_in_trajectory(t_xyz_wxyz):
    traj_has_nans = np.any(np.isnan(t_xyz_wxyz), axis=1)
    nan_percentage = np.count_nonzero(traj_has_nans) / len(traj_has_nans) * 100
    return nan_percentage, traj_has_nans


def timestamp_to_rosbag_time(timestamps, df_rt):
    return np.interp(timestamps, df_rt['ts_real'], df_rt['t_sim'])


def timestamp_to_rosbag_time_zero(timestamps, df_rt):
    return timestamp_to_rosbag_time(timestamps, df_rt) - df_rt['t_sim'][0]


def timestamp_to_real_time(timestamps, df_rt):
    return np.interp(timestamps, df_rt['ts_real'], df_rt['t_real'])


def envyaml_to_archive_dict(eval_config: EnvYAML) -> Dict:
    conf = eval_config.export()
    # remove environment keys to protect privacy
    for k in os.environ.keys():
        conf.pop(k)
    return conf


def name_to_identifier(name):
    return name.lower().replace(' ', '_')


def convert_eklt_to_rpg_tracks(df_tracks, file=None):
    tracks = df_tracks.loc[df_tracks.update_type != 'Lost', ['id', 'patch_t_current', 'center_x',
                                                             'center_y']].to_numpy()
    if file is not None:
        np.savetxt(file, tracks)
    return tracks


def convert_xvio_to_rpg_tracks(df_tracks, file=None):
    tracks = df_tracks[['id', 't', 'x_dist', 'y_dist']].to_numpy()
    if file is not None:
        np.savetxt(file, tracks)
    return tracks


# def read_json_file(output_folder):
#     profile_json_filename = os.path.join(output_folder, "profiling.json")
#     if os.path.exists(profile_json_filename):
#         with open(profile_json_filename, "rb") as f:
#             profiling_json = orjson.loads(f.read())
#     else:
#         profiling_json = None
#     return profiling_json


def read_output_files(output_folder, gt_available):
    df_poses = pd.read_csv(os.path.join(output_folder, "pose.csv"), delimiter=";")
    df_features = pd.read_csv(os.path.join(output_folder, "features.csv"), delimiter=";")
    df_resources = pd.read_csv(os.path.join(output_folder, "resource.csv"), delimiter=";")
    df_groundtruth = None
    if gt_available:
        df_groundtruth = pd.read_csv(os.path.join(output_folder, "gt.csv"), delimiter=";")
    df_realtime = pd.read_csv(os.path.join(output_folder, "realtime.csv"), delimiter=";")

    df_xvio_tracks = pd.read_csv(os.path.join(output_folder, "xvio_tracks.csv"), delimiter=";")

    df_imu_bias = None
    imu_bias_filename = os.path.join(output_folder, "imu_bias.csv")
    if os.path.exists(imu_bias_filename):
        df_imu_bias = pd.read_csv(imu_bias_filename, delimiter=";")


    df_ekf_updates = None
    ekf_updates_filename = os.path.join(output_folder, "ekf_updates.csv")
    if os.path.exists(ekf_updates_filename):
        df_ekf_updates = pd.read_csv(ekf_updates_filename, delimiter=";")

    # profiling_json = read_json_file(output_folder)
    return df_groundtruth, df_poses, df_realtime, df_features, df_resources, df_xvio_tracks, df_imu_bias, df_ekf_updates


def read_eklt_output_files(output_folder):
    df_events = pd.read_csv(os.path.join(output_folder, "events.csv"), delimiter=";")
    df_optimizations = pd.read_csv(os.path.join(output_folder, "optimizations.csv"), delimiter=";")
    df_eklt_tracks = pd.read_csv(os.path.join(output_folder, "eklt_tracks.csv"), delimiter=";")
    return df_events, df_optimizations, df_eklt_tracks


def rms(data):
    return np.linalg.norm(data) / np.sqrt(len(data))


def nanrms(data):
    without_nans = data[~np.isnan(data)]
    return np.linalg.norm(without_nans) / np.sqrt(len(without_nans))


def n_to_grid_size(n):
    cols = 1
    rows = 1
    while n > cols * rows:
        if cols - rows < 2:  # this number should adapt, but works fine up to ~30
            cols = cols + 1
        else:
            cols = cols - 1
            rows = rows + 1
    return rows, cols


class DynamicAttributes:
    def __getattr__(self, item):
        if item not in self.__dict__.keys():
            return None
        return self.__dict__[item]

    def __setattr__(self, key, value):
        self.__dict__[key] = value


def merge_tables(tables, column=None):
    result_table = None
    for t in tables:
        if result_table is None:
            result_table = t
        else:
            if column:
                result_table = pd.merge(result_table, t, on=column)
            else:
                result_table = pd.merge(result_table, t, left_index=True, right_index=True)
    return result_table


def get_quantized_statistics_along_axis(x, data, data_filter=None, resolution=0.1):
    # TODO: this code fails if min(x) == max(x)
    buckets = np.arange(np.min(x), np.max(x), resolution)
    bucket_index = np.digitize(x, buckets)
    indices = np.unique(bucket_index)

    # filter empty buckets:  (-1 to convert upper bound --> lower bound, as we always take the first errors per bucket)
    buckets = buckets[np.clip(indices - 1, 0, len(buckets))]

    stats_func = get_common_stats_functions()

    stats = {x: np.empty((len(indices))) for x in stats_func.keys()}

    for i, idx in enumerate(indices):
        data_slice = data[bucket_index == idx]

        if data_filter:
            data_slice = data_filter(data_slice)

        for k, v in stats.items():
            stats[k][i] = stats_func[k](data_slice)

    return buckets, stats


def get_common_stats_functions():
    stats_func = {
        'mean': lambda d: np.mean(d),
        'median': lambda d: np.median(d),
        'min': lambda d: np.min(d),
        'max': lambda d: np.max(d),
        'q25': lambda d: np.quantile(d, 0.25),
        'q75': lambda d: np.quantile(d, 0.75),
        'q05': lambda d: np.quantile(d, 0.05),
        'q95': lambda d: np.quantile(d, 0.95),
        'num': lambda d: len(d)
    }
    return stats_func


def read_neurobem_trajectory(filename):
    input_traj = pd.read_csv(filename)
    # zero align
    input_traj['t'] -= input_traj['t'][0]
    t_xyz_wxyz = input_traj[["t", "pos x", "pos y", "pos z", "quat w", "quat x", "quat y", "quat z"]].to_numpy()
    trajectory = PoseTrajectory3D(t_xyz_wxyz[:, 1:4], t_xyz_wxyz[:, 4:8], t_xyz_wxyz[:, 0])
    return trajectory


def read_x_evaluate_gt_csv(gt_csv_filename):
    df_groundtruth = pd.read_csv(gt_csv_filename, delimiter=";")
    evo_trajectory, _ = convert_to_evo_trajectory(df_groundtruth)
    return evo_trajectory


def read_esim_trajectory_csv(csv_filename):
    df_trajectory = pd.read_csv(csv_filename)
    # columns: ['# timestamp', ' x', ' y', ' z', ' qx', ' qy', ' qz', ' qw']

    df_trajectory['# timestamp'] /= 1e9

    t_xyz_wxyz = df_trajectory[['# timestamp', ' x', ' y', ' z', ' qw', ' qx', ' qy', ' qz']].to_numpy()
    return convert_t_xyz_wxyz_to_evo_trajectory(t_xyz_wxyz)


def get_ros_topic_name_from_msg_type(input_bag, msg_type: str):
    topic_info = input_bag.get_type_and_topic_info()
    event_topics = [k for k, t in topic_info.topics.items() if t.msg_type == msg_type]
    if len(event_topics) > 1:
        logging.warning("multiple event topics found (%s), taking first: '%s'", event_topics, event_topics[0])
    elif len(event_topics) == 0:
        raise LookupError("No dvs_msgs/EventArray found in bag")
    event_topic = event_topics[0]
    return event_topic


def read_all_ros_msgs_from_topic_into_dict(event_topic, input_bag):
    event_array_messages = {}
    for topic, msg, t in input_bag.read_messages([event_topic]):
        if t in event_array_messages:
            logging.warning("Multiple messages at time %s in topic %s", t, topic)
        event_array_messages[t.to_sec()] = msg
    return event_array_messages
