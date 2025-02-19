import argparse
import os
from copy import deepcopy
from typing import List

import numpy as np
import pandas as pd
import x_evaluate
from evo.core import metrics
from evo.core.metrics import PoseRelation
from matplotlib import pyplot as plt

from x_evaluate.comparisons import create_parameter_changes_table, identify_common_datasets
from x_evaluate.evaluation_data import FrontEnd, AlignmentType, EvaluationDataSummary
from x_evaluate.math_utils import calculate_velocities, moving_average_fixed_n, moving_average
from x_evaluate.plots import PlotContext, time_series_plot, plot_moving_boxplot_in_time_from_stats, DEFAULT_COLORS
from x_evaluate.rpg_trajectory_evaluation import rpg_align
from x_evaluate.scriptlets import find_evaluation_files_recursively, read_evaluation_pickle, cache
from x_evaluate.utils import merge_tables, get_quantized_statistics_along_axis, get_common_stats_functions, \
    name_to_identifier


def plot_ekf_updates_per_second(pc: PlotContext, chosen_ones: List[EvaluationDataSummary], dataset, t_max=None):
    times = []
    data = []
    labels = []
    for e in chosen_ones:
        labels.append(e.name)
        t, u = calculate_updates_per_seconds(e.data[dataset].df_ekf_updates, 5)

        if t_max is not None:
            u = u[t < t_max]
            t = t[t < t_max]
        times.append(t)
        data.append(u)
    time_series_plot(pc, times, data, labels, ylabel="EKF Updates/s")


def plot_ate_in_time(pc: PlotContext, chosen_ones: List[EvaluationDataSummary], dataset, t_max=None):
    times = []
    data = []
    labels = []
    translation_metric = metrics.APE(PoseRelation.translation_part)
    for e in chosen_ones:
        labels.append(e.name)
        t = e.data[dataset].trajectory_data.traj_gt_synced.timestamps
        t = t - t[0]
        error = e.data[dataset].trajectory_data.ate_errors[str(translation_metric)]

        if t_max is not None:
            error = error[t < t_max]
            t = t[t < t_max]

        times.append(t)
        data.append(error)
    time_series_plot(pc, times, data, labels, ylabel="Absolute position error [m]")


def plot_slam_features_in_time(pc: PlotContext, chosen_ones: List[EvaluationDataSummary], dataset, t_max=None):
    times = []
    data = []
    labels = []
    for e in chosen_ones:
        labels.append(e.name)
        t = e.data[dataset].feature_data.df_xvio_num_features['t']
        num_slam_features = e.data[dataset].feature_data.df_xvio_num_features['num_slam_features']

        if t_max is not None:
            num_slam_features = num_slam_features[t < t_max]
            t = t[t < t_max]
        times.append(t)
        data.append(num_slam_features)
    time_series_plot(pc, times, data, labels, ylabel="Number of SLAM features")


def plot_gt_dynamics(pc: PlotContext, summary: EvaluationDataSummary, datasets, t_max=None):
    times = []
    data = []
    labels = []
    for d in datasets:
        labels.append(d)
        t, lin_vel, ang_vel = calculate_velocities(summary.data[d].trajectory_data.traj_gt)

        t -= t[0]

        lin_vel = np.linalg.norm(lin_vel, axis=1)
        # ang_vel = np.linalg.norm(ang_vel, axis=1)

        if t_max is not None:
            lin_vel = lin_vel[t < t_max]
            t = t[t < t_max]

        times.append(t)
        data.append(lin_vel)
    time_series_plot(pc, times, data, labels, ylabel="Linear velocity [m/s]")


def main():
    parser = argparse.ArgumentParser(description="Takes all evaluation files in within the input directory tree and "
                                                 "analyzes ")
    parser.add_argument('--input_folder', type=str, required=True)

    args = parser.parse_args()

    pd.options.display.max_colwidth = None
    pd.options.display.width = 0

    evaluation_files = find_evaluation_files_recursively(args.input_folder)

    print(F"Found {evaluation_files}")

    def read_evaluations():
        evaluations = []

        for f in evaluation_files:
            if '2022-02-01' not in f:
                continue

            e = read_evaluation_pickle(os.path.dirname(f), os.path.basename(f))

            if e.frontend is not FrontEnd.EKLT:  # or ('3600 events' not in e.name and '3msec' not in e.name):
                continue

            print(F"Considering evaluation file '{e.name}'")

            evaluations.append(e)
        return evaluations

    print('reading...')
    evaluations: List[EvaluationDataSummary]
    evaluations = cache("/tmp/evaluations-speedup.pickle", read_evaluations)
    print('done')

    common_datasets = identify_common_datasets(evaluations)
    parameter_change_table = create_parameter_changes_table(evaluations, common_datasets)

    strategies = list(set(parameter_change_table.T['eklt_ekf_update_strategy']))
    strategies.sort()

    dataset_choice = ["Boxes 6DOF", "Boxes Translation", "Dynamic 6DOF", "Dynamic Translation", "HDR Boxes",
                      "HDR Poster", "Poster 6DOF", "Poster Translation", "Shapes 6DOF", "Shapes Translation"]

    target_datasets = list(common_datasets.intersection(set(dataset_choice)))
    target_datasets.sort()

    dataset = target_datasets[0]

    # with PlotContext(subplot_cols=len(strategies), subplot_rows=3) as pc:
    #
    #     for s in strategies:
    #         chosen_ones = [e for e in evaluations if e.data[dataset].params['eklt_ekf_update_strategy'] == s]
    #         plot_ekf_updates_per_second(pc, chosen_ones, dataset)
    #
    #     for s in strategies:
    #         chosen_ones = [e for e in evaluations if e.data[dataset].params['eklt_ekf_update_strategy'] == s]
    #         plot_ate_in_time(pc, chosen_ones, dataset)
    #
    #     for s in strategies:
    #         chosen_ones = [e for e in evaluations if e.data[dataset].params['eklt_ekf_update_strategy'] == s]
    #         plot_slam_features_in_time(pc, chosen_ones, dataset)
    #
    # for dataset in ["Boxes 6DOF", "Boxes Translation"]:
    #     with PlotContext(subplot_rows=3) as pc:
    #         pc.figure.suptitle(F"Evaluations on '{dataset}'")
    #         chosen_ones = [e for e in evaluations if "20ms" in e.name or "25000" in e.name]
    #         #or "15000" in e.name or
    #         #               "20000" in e.name or "5000" in e.name]
    #         plot_ekf_updates_per_second(pc, chosen_ones, dataset, 35)
    #         plot_ate_in_time(pc, chosen_ones, dataset, 35)
    #         plot_gt_dynamics(pc, chosen_ones[0], [dataset], 35)
    #
    # plt.show()

    x_evaluate.plots.use_paper_style_plots = True

    # Final paper plot
    dataset = "Boxes Translation"
    with PlotContext(os.path.join(args.input_folder, F"{name_to_identifier(dataset)}_update_strategy"),
                     subplot_rows=2) as pc:
        # pc.figure.suptitle(F"Evaluations on '{dataset}'")
        eval_20ms = [e for e in evaluations if "20ms" in e.name]
        eval_25k = [e for e in evaluations if "25000" in e.name]
        chosen_ones = eval_20ms + eval_25k
        #or "15000" in e.name or
        #               "20000" in e.name or "5000" in e.name]
        # plot_ekf_updates_per_second(pc, chosen_ones, dataset, 35)
        # plot_ate_in_time(pc, chosen_ones, dataset, 35)
        # plot_gt_dynamics(pc, chosen_ones[0], [dataset], 35)

        ax_top = pc.get_axis()
        ax_bottom = pc.get_axis(sharex=ax_top)

        ax_top.set_ylabel("EKF Updates / s")
        ax_bottom.set_ylabel("APE [m]")

        t_max = 35
        translation_metric = metrics.APE(PoseRelation.translation_part)

        labels = ["every 20ms", "every 25k events"]
        for j, e in enumerate(chosen_ones):
            t, u = calculate_updates_per_seconds(e.data[dataset].df_ekf_updates, 5)

            u = u[t < t_max]
            t = t[t < t_max]

            ax_top.plot(t, u, label=labels[j], color=DEFAULT_COLORS[j+3])

            t_error = e.data[dataset].trajectory_data.traj_gt_synced.timestamps
            t_error = t_error - t_error[0]

            error = e.data[dataset].trajectory_data.ate_errors[str(translation_metric)]

            error = error[t_error < t_max]
            t_error = t_error[t_error < t_max]

            ax_bottom.plot(t_error, error, label=labels[j], color=DEFAULT_COLORS[j+3])

        ax_bottom.set_xlabel("Time [s]")
        ax_top.legend()
        ax_top.label_outer()
        ax_bottom.label_outer()
        ax_bottom.axvspan(0, 15, alpha=0.1, facecolor=DEFAULT_COLORS[0])
        ax_top.axvspan(0, 15, alpha=0.1, facecolor=DEFAULT_COLORS[0])
        ax_bottom.axvspan(15, t_max, alpha=0.05, facecolor=DEFAULT_COLORS[1])
        ax_top.axvspan(15, t_max, alpha=0.05, facecolor=DEFAULT_COLORS[1])
        ax_bottom.set_xlim(0, t_max)

        ax_bottom.text(1, 0.63, "slow", style='italic')
        ax_bottom.text(16, 0.63, "fast", style='italic')

    # PlotContext.FORMATS = [".pdf"]
    #
    # slow_segment = [15, 25]
    # fast_segment = [0, 60]
    #
    # table_data = []
    # with PlotContext(subplot_cols=2, subplot_rows=len(target_datasets)) as pc:
    #     for dataset in target_datasets:
    #         data = evaluations[0].data[dataset]
    #
    #         t, lin_vel, ang_vel = calculate_velocities(data.trajectory_data.traj_gt)
    #
    #         t -= t[0]
    #
    #         lin_vel = np.linalg.norm(lin_vel, axis=1)
    #         ang_vel = np.linalg.norm(ang_vel, axis=1)
    #
    #         # ax = pc.get_axis()
    #         time_windows = [5, 10, 15, 20, 25]
    #         t_s, lin_vels = zip(*tuple([moving_average(t, lin_vel, time_window) for time_window in time_windows]))
    #         time_series_plot(pc, list(t_s), lin_vels, [F"{tw}s average" for tw in time_windows],
    #                          F"Linear velocity norm '{dataset}'", "[m/s]")
    #
    #         t_s, ang_vels = zip(*tuple([moving_average(t, ang_vel, time_window) for time_window in time_windows]))
    #         time_series_plot(pc, list(t_s), ang_vels, [F"{tw}s average" for tw in time_windows],
    #                          F"Angular velocity norm '{dataset}'", "[rad/s]")
    #
    #         i_1_slow = np.argmin(np.abs(t - slow_segment[0]))
    #         i_2_slow = np.argmin(np.abs(t - slow_segment[1]))
    #         i_1_fast = np.argmin(np.abs(t - fast_segment[0]))
    #         i_2_fast = np.argmin(np.abs(t - fast_segment[1]))
    #
    #         new_row = [np.mean(lin_vel[i_1_slow:i_2_slow+1]), np.mean(lin_vel[i_1_fast:i_2_fast+1]),
    #                    np.mean(ang_vel[i_1_slow:i_2_slow+1]), np.mean(ang_vel[i_1_fast:i_2_fast+1])]
    #
    #         table_data.append(new_row)
    #
    # index_columns = [("GT Stats", "Lin vel SLOW"),
    #                  ("GT Stats", "Lin vel FAST"),
    #                  ("GT Stats", "Ang vel SLOW"),
    #                  ("GT Stats", "Ang vel FAST")]
    # index = pd.MultiIndex.from_tuples(index_columns, names=["Evaluation Run", "Metric"])
    #
    # slow_fast_gt_stats_table = pd.DataFrame(table_data, index=target_datasets, columns=index)
    #
    #
    # # plt.show()
    #
    # print(slow_fast_gt_stats_table)
    #
    # updates_per_sec_data = {}
    #
    # stats_func = get_common_stats_functions()
    #
    # slow_fast_ekf_updates_tables = []
    #
    # for e in evaluations:
    #     labels = []
    #     times = []
    #     updates = []
    #     table_data = []
    #     for k in target_datasets:
    #         t_x, updates_per_sec = calculate_updates_per_seconds(e.data[k].df_ekf_updates)
    #         labels.append(k)
    #         times.append(t_x)
    #         updates.append(updates_per_sec)
    #
    #         i_1_slow = np.argmin(np.abs(t_x - slow_segment[0]))
    #         i_2_slow = np.argmin(np.abs(t_x - slow_segment[1]))
    #         i_1_fast = np.argmin(np.abs(t_x - fast_segment[0]))
    #         i_2_fast = np.argmin(np.abs(t_x - fast_segment[1]))
    #
    #         new_row = [np.mean(updates_per_sec[i_1_slow:i_2_slow + 1]), np.mean(updates_per_sec[i_1_fast:i_2_fast + 1])]
    #         table_data.append(new_row)
    #
    #     index_columns = [(e.name, "EKF Updates/s SLOW"),
    #                      (e.name, "EKF Updates/s FAST")]
    #     index = pd.MultiIndex.from_tuples(index_columns, names=["Evaluation Run", "Metric"])
    #     slow_fast_ekf_updates_table = pd.DataFrame(table_data, index=target_datasets, columns=index)
    #     slow_fast_ekf_updates_tables.append(slow_fast_ekf_updates_table)
    #
    #     updates_per_sec_data[e.name] = []
    #
    #     for func in stats_func.values():
    #         updates_per_sec_data[e.name].append(func(np.array(updates)))
    #
    #     with PlotContext() as pc:
    #         time_series_plot(pc, times, updates, labels, F"EKF updates per seconds '{e.name}'", "updates / s")
    #
    # slow_fast_ekf_updates_table = merge_tables(slow_fast_ekf_updates_tables)
    #
    # print(slow_fast_ekf_updates_table)
    #
    # #
    # #
    # # with PlotContext() as pc:
    # #     # ax = pc.get_axis()
    # #     df_ekf_updates = evaluations[0].data['Boxes 6DOF'].df_ekf_updates
    # #     t_x, updates_per_sec = calculate_updates_per_seconds(df_ekf_updates)
    # #
    # #     ax = pc.get_axis()
    # #
    # #     ax.plot(t_x, updates_per_sec)
    # #
    # #     # t_quantized, stats = get_quantized_statistics_along_axis(t[:-1]-t[0], 1/tdiff, resolution=0.1)
    # #     # plot_moving_boxplot_in_time_from_stats(pc, t_quantized, stats, F"Measurement based EKF updates/s '", "fps")
    #
    # slow_fast_pose_error_tables = []
    #
    # for evaluation in evaluations:
    #     table_data = np.empty((len(target_datasets), 4), dtype=np.float)
    #     i = 0
    #
    #     for dataset in target_datasets:
    #         data = evaluation.data[dataset]
    #
    #         traj_gt = data.trajectory_data.traj_gt_synced
    #         traj_est = data.trajectory_data.traj_est_synced
    #
    #         slow_part_est, slow_part_gt = slice_trajectories_into_segments(traj_est, traj_gt, slow_segment)
    #         fast_part_est, fast_part_gt = slice_trajectories_into_segments(traj_est, traj_gt, fast_segment)
    #
    #         print(F"Slow part lengths: {len(slow_part_gt.distances)} {len(slow_part_est.distances)}")
    #
    #         rpg_align(slow_part_gt, slow_part_est, AlignmentType.PosYaw, use_subtrajectory=False)
    #         rpg_align(fast_part_gt, fast_part_est, AlignmentType.PosYaw, use_subtrajectory=False)
    #
    #         slow_part_pos_result, slow_part_rot_result = calculate_pos_and_rot_errors(slow_part_est,
    #                                                                                   slow_part_gt)
    #
    #         fast_part_pos_result, fast_part_rot_result = calculate_pos_and_rot_errors(fast_part_est,
    #                                                                                   fast_part_gt)
    #
    #         table_data[i, :] = [slow_part_pos_result, fast_part_pos_result, slow_part_rot_result, fast_part_rot_result]
    #         i += 1
    #
    #     index_columns = [(evaluation.name, "Pos Error [m] SLOW"),
    #                      (evaluation.name, "Pos Error [m] FAST"),
    #                      (evaluation.name, "Rot Error [deg] SLOW"),
    #                      (evaluation.name, "Rot Error [deg] FAST")]
    #     index = pd.MultiIndex.from_tuples(index_columns, names=["Evaluation Run", "Metric"])
    #     slow_fast_ekf_updates_table = pd.DataFrame(table_data, index=target_datasets, columns=index)
    #     slow_fast_pose_error_tables.append(slow_fast_ekf_updates_table)
    #
    # slow_fast_ekf_updates_table = merge_tables(slow_fast_pose_error_tables)
    # print(slow_fast_ekf_updates_table)
    #
    # big_summary_table = merge_tables([slow_fast_gt_stats_table] + slow_fast_ekf_updates_tables +
    #                                      slow_fast_pose_error_tables)
    #
    # mask_trans = big_summary_table.index.str.contains('Translation')
    # mask_6dof = big_summary_table.index.str.contains('6DOF')
    # mask_hdr = big_summary_table.index.str.contains('HDR')
    # mean_trans = big_summary_table[mask_trans].mean()
    # mean_6dof = big_summary_table[mask_6dof].mean()
    # mean_hdr = big_summary_table[mask_hdr].mean()
    # mean = big_summary_table.mean()
    # median = big_summary_table.median()
    # big_summary_table.loc["* Translation"] = mean_trans
    # big_summary_table.loc["* 6DOF"] = mean_6dof
    # big_summary_table.loc["* HDR"] = mean_hdr
    # big_summary_table.loc["Mean"] = mean
    # big_summary_table.loc["Median"] = median
    #
    # with pd.ExcelWriter(os.path.join(args.input_folder, "update_strategy_result.xlsx")) as writer:
    #     big_summary_table.to_excel(writer, sheet_name='ALL WE NEED')
    #     slow_fast_ekf_updates_table.to_excel(writer, sheet_name='Average pose errors')
    #     slow_fast_ekf_updates_table.to_excel(writer, sheet_name='EKF updates per second')
    #     slow_fast_gt_stats_table.to_excel(writer, sheet_name='GT dynamics stats')
    #
    # plt.show()


def slice_trajectories_into_segments(traj_est, traj_gt, segment_timestamps):
    slow_part_gt = deepcopy(traj_gt)
    ids_slow = np.where(
        np.logical_and(slow_part_gt.timestamps >= traj_gt.timestamps[0] + segment_timestamps[0],
                       slow_part_gt.timestamps <= traj_gt.timestamps[0] + segment_timestamps[1]))[0]
    slow_part_gt.reduce_to_ids(ids_slow)
    slow_part_est = deepcopy(traj_est)
    slow_part_est.reduce_to_ids(ids_slow)
    return slow_part_est, slow_part_gt


def calculate_pos_and_rot_errors(slow_part_est, slow_part_gt):
    position_metric = metrics.APE(metrics.PoseRelation.translation_part)
    orientation_metric = metrics.APE(metrics.PoseRelation.rotation_angle_deg)
    position_metric.process_data((slow_part_gt, slow_part_est))
    orientation_metric.process_data((slow_part_gt, slow_part_est))
    slow_part_pos_result = position_metric.get_result().stats['mean'] / slow_part_gt.distances[-1] * 100
    slow_part_rot_result = orientation_metric.get_result().stats['mean'] / slow_part_gt.distances[-1]
    return slow_part_pos_result, slow_part_rot_result


def calculate_updates_per_seconds(df_ekf_updates, n_points_per_second=1):
    t_ekf = df_ekf_updates['t'].to_numpy()
    # tdiff = t[1:] - t[:-1]
    # ax.plot(t[:-1], 1/tdiff)
    t_ekf -= t_ekf[0]
    bins = np.arange(0.0, t_ekf[-1], 1.0)


    updates_per_sec_all = np.array([])
    t_all = np.array([])

    for j in range(n_points_per_second):
        updates_per_sec, _ = np.histogram(t_ekf, bins=bins)

        updates_per_sec_all = np.hstack((updates_per_sec_all, updates_per_sec))
        t = np.arange(0.0, len(updates_per_sec))
        t = t + j * 1.0 / n_points_per_second
        t_all = np.hstack((t_all, t))

        # move for next iteration
        bins = bins + 1.0 / n_points_per_second

    idx = np.argsort(t_all)
    t_all = t_all[idx]
    updates_per_sec_all = updates_per_sec_all[idx]
    return t_all, updates_per_sec_all


if __name__ == '__main__':
    main()
