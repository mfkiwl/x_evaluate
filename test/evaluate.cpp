//
// Created by Florian Mahlknecht on 2021-03-15.
// Copyright (c) 2021 NASA / JPL. All rights reserved.

#include <gflags/gflags.h>
#include <rosbag/bag.h>
#include <rosbag/view.h>
#include <tf2_msgs/TFMessage.h>
#include <iostream>
#if __has_include(<filesystem>)
  #include <filesystem>
  namespace fs = std::filesystem;
#elif __has_include(<experimental/filesystem>)
  #include <experimental/filesystem>
  namespace fs = std::experimental::filesystem;
#else
  error "Missing the <filesystem> header."
#endif
#include <memory>
#include <yaml-cpp/yaml.h>
#include <easy/profiler.h>
#include <cv_bridge/cv_bridge.h>

#include <x/vio/parameter_loader.h>
#include <x/vio/abstract_vio.h>
#include <x/vio/vio.h>
#include <x/eklt/types.h>
#include <x/eklt/eklt_vio.h>
#include <x/haste/haste_vio.h>
#include <x/events/e_vio.h>
#include <x/common/csv_writer.h>

#include <geometry_msgs/PoseStamped.h>
#include <dvs_msgs/EventArray.h>
#include <sensor_msgs/Image.h>
#include <sensor_msgs/Imu.h>

// @see https://stackoverflow.com/a/58328388 and https://github.com/boostorg/timer/issues/12
#define BOOST_ALLOW_DEPRECATED_HEADERS
#include <boost/progress.hpp>
#undef BOOST_ALLOW_DEPRECATED_HEADERS

#include <sys/resource.h>
#include <ctime>



/**********************************************************************************************************************
 *     Handy functions from ros_utils.cpp (x_vio_ros) --> (ugly duplication but avoids dependency on x_vio_ros)       *
 **********************************************************************************************************************/

x::EventArray::Ptr msgToEvents(const dvs_msgs::EventArrayConstPtr &events_msg_ptr) {
  std::vector<x::Event> event_list;
  event_list.reserve(events_msg_ptr->events.size());

  for (const auto& e : events_msg_ptr->events) {
    event_list.emplace_back(e.x, e.y, e.ts.toSec(), e.polarity);
  }

  x::EventArray::Ptr x_events(new x::EventArray(events_msg_ptr->header.seq, events_msg_ptr->height,
                                                events_msg_ptr->width, event_list));
  return x_events;
}

cv_bridge::CvImageConstPtr msgToImage(const sensor_msgs::ImageConstPtr &img) {
  cv_bridge::CvImageConstPtr cv_ptr;
  try
  {
    cv_ptr = cv_bridge::toCvShare(img, sensor_msgs::image_encodings::MONO8);
  }
  catch (cv_bridge::Exception& e)
  {
    ROS_ERROR("cv_bridge exception: %s", e.what());
  }
  return cv_ptr;
}

bool msgToTiledImage(const x::Params& params, const sensor_msgs::ImageConstPtr &img, x::TiledImage &tiledImage) {

  auto cv_ptr = msgToImage(img);
  if (!cv_ptr)
    return false;

  const unsigned int frame_number = img->header.seq;
  auto timestamp = img->header.stamp.toSec();

  // Shallow copies
  tiledImage = x::TiledImage(cv_ptr->image, timestamp, frame_number,
                             params.n_tiles_h, params.n_tiles_w, params.max_feat_per_tile);

  return true;
}

inline x::Vector3 msgVector3ToEigen(geometry_msgs::Vector3 vector) {
  return { vector.x, vector.y, vector.z };
}



/**********************************************************************************************************************
 *                                  Command line arguments definition and processing                                  *
 **********************************************************************************************************************/


enum class Frontend : int8_t {
  XVIO = 0,
  EKLT = 1,
  EVIO = 2,
  HASTE = 3,
};

std::map<std::string, Frontend> frontends {
  {"XVIO", Frontend::XVIO},
  {"EKLT", Frontend::EKLT},
  {"EVIO", Frontend::EVIO},
  {"HASTE", Frontend::HASTE},
};


DEFINE_string(input_bag, "", "filename of the bag to scan");
DEFINE_string(events_topic, "", "topic in rosbag publishing dvs_msgs::EventArray");
DEFINE_string(image_topic, "/cam0/image_raw", "topic in rosbag publishing sensor_msgs::Image");
DEFINE_string(pose_topic, "", "(optional) topic publishing IMU pose ground truth as geometry_msgs::PoseStamped");
DEFINE_string(imu_topic, "/imu", "topic in rosbag publishing sensor_msgs::Imu");
DEFINE_string(params_file, "", "filename of the params.yaml to use");
DEFINE_string(output_folder, "", "folder where to write output files, is created if not existent");
DEFINE_double(from, std::numeric_limits<double>::min(), "skip messages with timestamp lower than --form");
DEFINE_double(to, std::numeric_limits<double>::max(), "skip messages with timestamp bigger than --to");
DEFINE_bool(dump_input_frames, false, "Whether to log input frames to disk");
DEFINE_bool(dump_debug_frames, false, "Whether to log debug frames to disk");


static bool validateFrontend(const char* flagname, const std::string& value) {
  if (frontends.find(value) != frontends.end())
    return true;
  std::string possible_values;
  bool is_first = true;
  for (const auto& v : frontends) {
    if (is_first) {
      possible_values += v.first;
      is_first = false;
    } else {
      possible_values += ", " + v.first;
    }
  }
  std::cerr << "Invalid error for '" << flagname << "'. Possible values: " << possible_values << std::endl;
  return false;
}

DEFINE_string(frontend, "XVIO", "which frontend to use");
DEFINE_validator(frontend, &validateFrontend);


using PoseCsv = x::CsvWriter<std::string,
                             double,
                             double, double, double,
                             double, double, double, double>;
using ImuBiasCsv = x::CsvWriter<double,
                             double, double, double,
                             double, double, double,
                             double, double, double,
                             double, double, double>;

using GTCsv = x::CsvWriter<double,
                           double, double, double,
                           double, double, double, double>;

void addPose(PoseCsv& csv, const std::string& update_modality, const x::State& s) {
  csv.addRow(update_modality, s.getTime(),
             s.getPosition().x(), s.getPosition().y(), s.getPosition().z(),
             s.getOrientation().x(), s.getOrientation().y(), s.getOrientation().z(), s.getOrientation().w());
}

void addImuBias(ImuBiasCsv& csv, const std::string& update_modality, const x::State& s) {
  // THIS CURRENTLY LEADS TO A SEGMENTATION FAULT ON poster_translation after 50% for some mysterious reason
//  const x::Matrix& imu_bias_cov = s.getDynamicCovariance().bottomRightCorner<6, 6>();
//  const double& sigma_w_x = imu_bias_cov(0, 0);
//  const double& sigma_w_y = imu_bias_cov(1, 1);
//  const double& sigma_w_z = imu_bias_cov(2, 2);
//  const double& sigma_a_x = imu_bias_cov(3, 3);
//  const double& sigma_a_y = imu_bias_cov(4, 4);
//  const double& sigma_a_z = imu_bias_cov(5, 5);
  const double sigma_w_x = 0.0;
  const double sigma_w_y = 0.0;
  const double sigma_w_z = 0.0;
  const double sigma_a_x = 0.0;
  const double sigma_a_y = 0.0;
  const double sigma_a_z = 0.0;
  csv.addRow(s.getTime(),
             s.getAccelerometerBias().x(), s.getAccelerometerBias().y(), s.getAccelerometerBias().z(),
             s.getGyroscopeBias().x(), s.getGyroscopeBias().y(), s.getGyroscopeBias().z(),
             sigma_w_x, sigma_w_y, sigma_w_z, sigma_a_x, sigma_a_y, sigma_a_z);
}

char* get_time_string_in_utc() {
  std::time_t curr_time;
  curr_time = std::time(nullptr);
  tm *tm_gmt = std::gmtime(&curr_time);
  return std::asctime(tm_gmt);
}


int evaluate(x::AbstractVio &vio, const fs::path &output_path, const x::Params& params) {
  PoseCsv pose_csv(output_path / "pose.csv", {"update_modality", "t",
                                              "estimated_p_x", "estimated_p_y", "estimated_p_z",
                                              "estimated_q_x", "estimated_q_y", "estimated_q_z", "estimated_q_w"});
  ImuBiasCsv imu_bias_csv(output_path / "imu_bias.csv", {"t", "b_a_x", "b_a_y", "b_a_z", "b_w_x", "b_w_y", "b_w_z",
                                                         "sigma_b_a_x", "sigma_b_a_y", "sigma_b_a_z", "sigma_b_w_x", "sigma_b_w_y", "sigma_b_w_z"});


  std::unique_ptr<GTCsv> gt_csv(nullptr);

  if (!FLAGS_pose_topic.empty())
    gt_csv.reset(new GTCsv(output_path / "gt.csv", {"t", "p_x", "p_y", "p_z", "q_x", "q_y", "q_z", "q_w"}));

  x::CsvWriter<double, double, profiler::timestamp_t, std::string, profiler::timestamp_t> rt_csv(
    output_path / "realtime.csv", {"t_sim", "t_real", "ts_real", "processing_type", "process_time_in_us"});

  x::CsvWriter<profiler::timestamp_t, double, double, double, size_t, size_t> resource_csv(
    output_path / "resource.csv",
    {"ts", "cpu_usage", "cpu_user_mode_usage", "cpu_kernel_mode_usage", "memory_usage_in_bytes", "debug_memory_in_bytes"});


  std::cerr << "Reading rosbag '" << FLAGS_input_bag << "'" << std::endl;
  rosbag::Bag bag;
  bag.open(FLAGS_input_bag);  // BagMode is Read by default

  vio.setUp(params);

  auto from = ros::TIME_MIN;
  auto to = ros::TIME_MAX;

  // if initialized differently from default values
  if (FLAGS_from > std::numeric_limits<double>::min())
    from = ros::Time(FLAGS_from);
  if (FLAGS_to < std::numeric_limits<double>::max())
    to = ros::Time(FLAGS_to);

  rosbag::View view(bag, from, to);

  std::cerr << "Initializing at time " << std::setprecision(17) << view.getBeginTime().toSec() << std::endl;
  vio.initAtTime(view.getBeginTime().toSec());

  std::cerr << "Processing rosbag from time " << view.getBeginTime() << " to " << view.getEndTime()
            << std::endl << std::endl;

  uint64_t counter_imu = 0, counter_image = 0, counter_events = 0, counter_pose = 0;
  bool filer_initialized = false;

  x::State state;
  auto t_0 = std::numeric_limits<double>::infinity();
  auto t_last_flush = std::numeric_limits<double>::infinity();
  boost::progress_display show_progress(view.size(), std::cerr);

  profiler::timestamp_t calculation_time = 0, last_rusage_check = 0;

  struct timeval rusage_walltime;
  gettimeofday(&rusage_walltime, nullptr);

  struct rusage prev_rusage;
  getrusage(RUSAGE_SELF, &prev_rusage);


  EASY_PROFILER_ENABLE;
  EASY_MAIN_THREAD;

  for (rosbag::MessageInstance const &m : view) {

    std::string process_type;

    auto start = profiler::now();

    if (m.getTopic() == FLAGS_imu_topic) {
      process_type = "IMU";
      auto msg = m.instantiate<sensor_msgs::Imu>();
      ++counter_imu;

      // count what would end up in ros callback
      EASY_BLOCK("IMU Message");

      auto a_m = msgVector3ToEigen(msg->linear_acceleration);
      auto w_m = msgVector3ToEigen(msg->angular_velocity);

      state = vio.processImu(msg->header.stamp.toSec(), msg->header.seq, w_m, a_m);
      EASY_END_BLOCK;

    } else if (m.getTopic() == FLAGS_image_topic) {
      process_type = "Image";
      auto msg = m.instantiate<sensor_msgs::Image>();
      ++counter_image;

      EASY_BLOCK("Image Message");
      x::TiledImage image;
      if (!msgToTiledImage(params, msg, image))
        continue;

      if (image.rows != params.img_height || image.cols != params.img_width) {
        std::cerr << "CRITICAL ERROR: image width different height / width encountered: " << image.cols << "x"
                  << image.rows << ", expected: " << params.img_width << "x" << params.img_height
                  << " --> SKIPPING IMAGE" << std::endl;
        continue;
      }

      x::TiledImage feature_img(image);
      state = vio.processImageMeasurement(image.getTimestamp(), image.getFrameNumber(), image, feature_img);
      EASY_END_BLOCK;

    } else if (vio.doesProcessEvents() && !FLAGS_events_topic.empty() && m.getTopic() == FLAGS_events_topic) {
      process_type = "Events";
      auto msg = m.instantiate<dvs_msgs::EventArray>();
      ++counter_events;

      EASY_BLOCK("Events Message");
      x::EventArray::Ptr x_events = msgToEvents(msg);

      x::TiledImage tracker_img, feature_img;
      state = vio.processEventsMeasurement(x_events, tracker_img, feature_img);
      EASY_END_BLOCK;

    } else if (!FLAGS_pose_topic.empty() && m.getTopic() == FLAGS_pose_topic) {
      EASY_BLOCK("GT Message");
      if (m.isType<geometry_msgs::PoseStamped>()) {
        auto p = m.instantiate<geometry_msgs::PoseStamped>();
        ++counter_pose;
        gt_csv->addRow(p->header.stamp.toSec(), p->pose.position.x, p->pose.position.y, p->pose.position.z,
                       p->pose.orientation.x, p->pose.orientation.y, p->pose.orientation.z, p->pose.orientation.w);
      } else if (m.isType<tf2_msgs::TFMessage>()) {
        auto tf = m.instantiate<tf2_msgs::TFMessage>();
        for (const auto & p : tf->transforms) {
          ++counter_pose;
          gt_csv->addRow(p.header.stamp.toSec(), p.transform.translation.x, p.transform.translation.y, p.transform.translation.z,
                         p.transform.rotation.x, p.transform.rotation.y, p.transform.rotation.z, p.transform.rotation.w);
        }

      } else {
        std::cerr << "WARNING: unable to type of GT message: " << m.getTopic() << std::endl;
      }
      EASY_END_BLOCK;
    }

    // stop here --> all the rest is not considered
    auto stop = profiler::now();

    if (m.getTime().toSec() < t_0)
      t_0 = m.getTime().toSec();

    if (m.getTime().toSec() < t_last_flush)  // initialization
      t_last_flush = m.getTime().toSec();

    if (m.getTime().toSec() - t_last_flush > 5.0) {
      t_last_flush = m.getTime().toSec();
      x::DebugMemoryMonitor::instance().flush_all();
    }

//    // profile 1s only to avoid huge files that are not handleable anymore
//    if (m.getTime().toSec() - t_0 > 1.0)
//      EASY_PROFILER_DISABLE;

    if (calculation_time - last_rusage_check >= 1000000) {
      last_rusage_check = calculation_time;
      struct timeval rusage_walltime_new;
      gettimeofday(&rusage_walltime_new, nullptr);

      double walltime_sec_passed = (rusage_walltime_new.tv_sec + rusage_walltime_new.tv_usec * 1e-6) -
                                 (rusage_walltime.tv_sec + rusage_walltime.tv_usec * 1e-6);

      struct rusage cur_rusage;
      getrusage(RUSAGE_SELF, &cur_rusage);

      double cpu_time_usr = (cur_rusage.ru_utime.tv_sec + cur_rusage.ru_utime.tv_usec * 1e-6) -
                            (prev_rusage.ru_utime.tv_sec + prev_rusage.ru_utime.tv_usec * 1e-6);
      double cpu_time_sys = (cur_rusage.ru_stime.tv_sec + cur_rusage.ru_stime.tv_usec * 1e-6) -
                            (prev_rusage.ru_stime.tv_sec + prev_rusage.ru_stime.tv_usec * 1e-6);

//        std::cout << "timings passed: WT: " << walltime_sec_passed
//                  << " USR: " << cpu_time_usr
//                  << " SYS: " << cpu_time_sys << std::endl;

      double cpu_usage = 100 * (cpu_time_sys + cpu_time_usr) / walltime_sec_passed;
      double cpu_usage_usr = 100 * cpu_time_usr / walltime_sec_passed;
      double cpu_usage_sys = 100 * cpu_time_sys / walltime_sec_passed;

      size_t mem_usage_in_bytes = cur_rusage.ru_maxrss * 1024L;
      size_t mem_usage_debug = x::DebugMemoryMonitor::instance().memory_usage_in_bytes();

      resource_csv.addRow(profiler::now(), cpu_usage, cpu_usage_usr, cpu_usage_sys, mem_usage_in_bytes, mem_usage_debug);

      rusage_walltime = rusage_walltime_new;
      prev_rusage = cur_rusage;
    }

    if (!filer_initialized && vio.isInitialized()) {
      filer_initialized = true;
//        auto count = show_progress.count();
//        show_progress.restart(view.size());
//        show_progress += count;
    }

    if (!process_type.empty() && filer_initialized) {
      auto duration_in_us = profiler::toMicroseconds(stop - start);
      calculation_time += duration_in_us;

      addPose(pose_csv, process_type, state);
      addImuBias(imu_bias_csv, process_type, state);
      rt_csv.addRow(m.getTime().toSec(), calculation_time * 1e-6, profiler::now(), process_type, duration_in_us);
    }

    ++show_progress;
  }

  profiler::dumpBlocksToFile((output_path / "profiling.prof").c_str());
//    JsonExporter je;
//    je.convert((output_path / "profiling.prof").c_str(), (output_path / "profiling.json").c_str());

  std::cerr << "Processed " << counter_imu << " IMU, "
            << counter_image << " image, "
            << counter_events << " event and "
            << counter_pose << " pose messages" << std::endl;

  std::cerr << "Writing outputs to folder " << output_path << std::endl;

  // manually flush to ensure files are dumped even in case of memory corruption (double-free in destructors etc.)
  x::DebugMemoryMonitor::instance().flush_all();

  bag.close();

  std::cerr << "Evaluation completed " << get_time_string_in_utc();
  std::cerr << "Good bye!" << std::endl;
  return 0;
}


int main(int argc, char **argv) {

  std::cerr << "Running " << argv[0] << " " << get_time_string_in_utc() << std::endl;

  google::ParseCommandLineFlags(&argc, &argv, true);

  if (FLAGS_output_folder.empty()) {
    std::cerr << "ERROR: No output folder specified, provide --output_folder" << std::endl;
    return 1;
  }

  // directly reads yaml file, without the need for a ROS master / ROS parameter server
  YAML::Node config = YAML::LoadFile(FLAGS_params_file);
  x::ParameterLoader l;
  x::Params params;
  auto success = l.loadXParams(params, [config](const std::string& key, auto &par) -> bool {
    if (config[key].IsDefined()) {
      // this is ugly but the only way to get the correct auto type for the template interface as<T>
      const auto& v = config[key].as<std::remove_reference_t<decltype(par)>>();
      par = v;
      return true;
    }
    return false;
  });

  std::cerr << "Reading config '" << FLAGS_params_file << "' was " << (success ? "successful" : "failing")
            << std::endl;

  if (!success)
    return 1;

  fs::path output_path(FLAGS_output_folder);
  fs::create_directories(output_path);
  fs::copy(FLAGS_params_file, output_path / "params.yaml", fs::copy_options::overwrite_existing);

  x::XVioPerformanceLoggerPtr xvio_logger =
    std::make_shared<x::XVioPerformanceLogger>(output_path, FLAGS_dump_input_frames, FLAGS_dump_debug_frames);


  switch(frontends[FLAGS_frontend]) {
    case Frontend::XVIO: {
      x::VIO vio(xvio_logger);
      return evaluate(vio, output_path, params);
    }
    case Frontend::EKLT: {
      x::EkltPerformanceLoggerPtr eklt_logger = std::make_shared<x::EkltPerformanceLogger>(output_path);
      x::EventsPerformanceLoggerPtr events_logger = std::make_shared<x::EventsPerformanceLogger>(output_path);
      x::EKLTVIO vio(xvio_logger, events_logger, eklt_logger);
      return evaluate(vio, output_path, params);
    }
    case Frontend::HASTE: {
      x::EventsPerformanceLoggerPtr events_logger = std::make_shared<x::EventsPerformanceLogger>(output_path);
      x::HASTEVIO vio(xvio_logger, events_logger);
      return evaluate(vio, output_path, params);
    }
    case Frontend::EVIO: {
      x::EVIO vio(xvio_logger);
      return evaluate(vio, output_path, params);
    }

    default:
      std::cerr << "Invalid frontend type, unable to evaluate" << std::endl;
      return 1;
  }
}
