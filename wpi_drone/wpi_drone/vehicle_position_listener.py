#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from px4_msgs.msg import VehicleOdometry, OffboardControlMode, TrajectorySetpoint, VehicleCommand, VehicleLocalPosition, VehicleStatus, BatteryStatus
from datetime import datetime
import math

# most coordinates are in NED (North, East, Down) frame, local coordinate offset inputs are in FLU (Forward, Left, Up) frame
class VehicleGlobalPositionListener(Node):
    def __init__(self):
        super().__init__('vehicle_global_position_listener')
        
        # Configure QoS profile for PX4
        qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,  # maybe TRANSIENT_LOCAL
            history=HistoryPolicy.KEEP_LAST,
            depth=5
        )
        
        # Debug logging
        debug_printout = True
        if debug_printout:
            self.get_logger().info("Starting subscriber with QoS profile:")
            self.get_logger().info(f"Reliability: {qos_profile.reliability}")
            self.get_logger().info(f"Durability: {qos_profile.durability}")
            self.get_logger().info(f"History: {qos_profile.history}")
            self.get_logger().info(f"Depth: {qos_profile.depth}")

        # Internal state tracking
        self.isArmed = False
        self.isFlying = False
        self.isOffboard = False
        self.arm_timestamp = None
        self.flight_start_timestamp = None
        self.initialization_counter = 0     # after detecting vehicle is in onboard mode, wait for 5 seconds before taking autonomous control
        self.home_coord_offset = [0.0, 0.0, 0.0]
        self.last_traj_setpoint_msg = None        
        self.status_var_temp = 0

        # PX4 publishers
        self.offboard_control_mode_publisher = self.create_publisher(   # heartbeat
            OffboardControlMode, '/fmu/in/offboard_control_mode', qos_profile)
        self.vehicle_command_publisher = self.create_publisher(         
            VehicleCommand, '/fmu/in/vehicle_command', qos_profile)
        self.trajectory_setpoint_publisher = self.create_publisher(     # offboard position sender
            TrajectorySetpoint, '/fmu/in/trajectory_setpoint', qos_profile)

        # PX4 subscribers
        self.vehicle_status = None
        self.vehicle_local_position = VehicleLocalPosition()
        self.vehicle_odometry = None
        self.battery_status = None
        
        self.vehicle_status_subscriber = self.create_subscription( 
            VehicleStatus, '/fmu/out/vehicle_status', self.vehicle_status_callback, qos_profile)
        self.vehicle_local_position_subscriber = self.create_subscription(
            VehicleLocalPosition, '/fmu/out/vehicle_local_position', self.vehicle_local_position_callback, qos_profile)    
        self.vehicle_odometry_subscriber = self.create_subscription(
            VehicleOdometry, '/fmu/out/vehicle_odometry', self.vehicle_odometry_callback, qos_profile)
        self.battery_status_subscriber = self.create_subscription(
            BatteryStatus, '/fmu/out/battery_status', self.battery_status_callback, qos_profile)
        
        # Create a timer to publish control commands
        self.timer = self.create_timer(0.1, self.timer_callback)
        
        if debug_printout:
            self.timer_debug = self.create_timer(2.0, self.timer_debug_callback)

    # https://docs.px4.io/main/en/msg_docs/VehicleLocalPosition.html
    #     px4_msgs.msg.VehicleLocalPosition(
    #     timestamp=1738781937874852,
    #     timestamp_sample=1738781937874186,
    #     xy_valid=True,
    #     z_valid=True,
    #     v_xy_valid=True,
    #     v_z_valid=True,
    #     x=-0.21537010371685028,
    #     y=0.09170171618461609,
    #     z=-0.019485270604491234,
    #     delta_xy=array([5.0181836e-05, 2.7746145e-04], dtype=float32),
    #     xy_reset_counter=2,
    #     delta_z=0.0,
    #     z_reset_counter=1,
    #     vx=0.0025058595929294825,
    #     vy=0.005207868292927742,
    #     vz=-0.003228991525247693,
    #     z_deriv=-0.001445640460588038,
    #     delta_vxy=array([0.00742345, 0.00078792], dtype=float32),
    #     vxy_reset_counter=2,
    #     delta_vz=-0.00030795918428339064,
    #     vz_reset_counter=1,
    #     ax=0.020625105127692223,
    #     ay=0.04751252010464668,
    #     az=-0.09083916246891022,
    #     heading=1.1044014692306519,
    #     heading_var=0.005346738267689943,
    #     unaided_heading=0.009717458859086037,
    #     delta_heading=1.360266923904419,
    #     heading_reset_counter=1,
    #     heading_good_for_control=False,
    #     tilt_var=6.0623140598181635e-05,
    #     xy_global=False,
    #     z_global=False,
    #     ref_timestamp=0,
    #     ref_lat=nan,
    #     ref_lon=nan,
    #     ref_alt=nan,
    #     dist_bottom_valid=True,
    #     dist_bottom=0.1713714599609375,
    #     dist_bottom_var=0.000816226121969521,
    #     delta_dist_bottom=0.0,
    #     dist_bottom_reset_counter=1,
    #     dist_bottom_sensor_bitfield=1,
    #     eph=0.5449826717376709,
    #     epv=0.03457729145884514,
    #     evh=0.026098940521478653,
    #     evv=0.032592181116342545,
    #     dead_reckoning=False,
    #     vxy_max=0.68548583984375,
    #     vz_max=inf,
    #     hagl_min=0.07999999821186066,
    #     hagl_max=7.199999809265137
    # )
    def vehicle_local_position_callback(self, vehicle_local_position):
        """Callback function for vehicle_local_position topic subscriber."""
        self.vehicle_local_position = vehicle_local_position        
    
    # https://docs.px4.io/main/en/msg_docs/VehicleStatus.html
    # px4_msgs.msg.VehicleStatus(
    #     timestamp=1738782750834039,
    #     armed_time=0,
    #     takeoff_time=0,
    #     arming_state=1,
    #     latest_arming_reason=13,
    #     latest_disarming_reason=6,
    #     nav_state_timestamp=1448486364,
    #     nav_state_user_intention=2,
    #     nav_state=2,
    #     executor_in_charge=0,
    #     valid_nav_states_mask=2147411071,
    #     can_set_nav_states_mask=8307839,
    #     failure_detector_status=0,
    #     hil_state=0,
    #     vehicle_type=1,
    #     failsafe=False,
    #     failsafe_and_user_took_over=False,
    #     failsafe_defer_state=0,
    #     gcs_connection_lost=False,
    #     gcs_connection_lost_counter=1,
    #     high_latency_data_link_lost=False,
    #     is_vtol=False,
    #     is_vtol_tailsitter=False,
    #     in_transition_mode=False,
    #     in_transition_to_fw=False,
    #     system_type=2,
    #     system_id=1,
    #     component_id=1,
    #     safety_button_available=True,
    #     safety_off=True,
    #     power_input_valid=True,
    #     usb_connected=False,
    #     open_drone_id_system_present=False,
    #     open_drone_id_system_healthy=False,
    #     parachute_system_present=False,
    #     parachute_system_healthy=False,
    #     avoidance_system_required=False,
    #     avoidance_system_valid=False,
    #     rc_calibration_in_progress=False,
    #     calibration_enabled=False,
    #     pre_flight_checks_pass=True
    # )
    def vehicle_status_callback(self, vehicle_status):
        """Callback function for vehicle_status topic subscriber."""
        # self.get_logger().info(f"Vehicle status: {vehicle_status.takeoff_time}")  # used for development prints
        self.vehicle_status = vehicle_status
        self.isArmed = vehicle_status.arming_state == 2 # armed
        self.isFlying = vehicle_status.takeoff_time > 0 # flying
        self.isOffboard = vehicle_status.nav_state == 14 # offboard
        
        # save the timestamp of the first time the vehicle is armed
        if self.isArmed:
            if self.arm_timestamp is None:
                self.arm_timestamp = vehicle_status.timestamp
        else:
            self.arm_timestamp = None
            self.initialization_counter = 0
            
        # save the timestamp and home coordinate offset
        if self.isFlying:
            if self.flight_start_timestamp is None:
                self.flight_start_timestamp = vehicle_status.timestamp
                self.home_coord_offset = [self.vehicle_local_position.x, self.vehicle_local_position.y, self.vehicle_local_position.z]
        else:
            self.flight_start_timestamp = None
            
    # https://docs.px4.io/main/en/msg_docs/VehicleOdometry.html
    def vehicle_odometry_callback(self, msg):
        """Callback function for vehicle_odometry topic subscriber."""
        self.vehicle_odometry = msg

    # https://docs.px4.io/main/en/msg_docs/BatteryStatus.html
    def battery_status_callback(self, msg):
        """Callback function for battery_status topic subscriber."""
        self.battery_status = msg

    def timer_callback(self) -> None:
        """Callback function for the timer."""
        # Publish the heartbeat
        self.publish_offboard_control_heartbeat_signal()

        # Wait for vehicle status to be initialized
        if self.vehicle_status is None:
            return
                    
        # Wait for vehicle to be armed, flying, and in offboard mode
        if self.isArmed and self.isFlying and self.isOffboard:
            if self.initialization_counter >= 50:
                if self.status_var_temp == 0:
                    self.get_logger().info("Vehicle is armed, flying, and in offboard mode")
                    self.status_var_temp = 1
                    
                elif self.status_var_temp == 1:
                    # move the vehicle half a meter to the right in the global, keeping the current yaw
                    # get the position of the vehicle in NED (X North, Y East, Z Down) local frame    
                    self.get_logger().info(f"Current position: {self.get_current_ned_pos()}")                                     
                    offset = self.ned_point_from_flu_offset(self.get_current_ned_pos(), [0.0, -1.0, 0.0])               
                    self.publish_trajectory_setpoint(offset)
                    self.status_var_temp = 2                    
                    
                elif self.status_var_temp == 2:                    
                    # check if the vehicle is at the target position
                    if self.is_at_traj_setpoint(0.05):
                        self.get_logger().info("Vehicle is at target position")                    
                        self.status_var_temp = 3
                        
                elif self.status_var_temp == 3:
                    # go back
                    self.get_logger().info(f"Current position: {self.get_current_ned_pos()}")
                    offset = self.ned_point_from_flu_offset(self.get_current_ned_pos(), [0.0, 1.0, 0.0])
                    self.publish_trajectory_setpoint(offset)
                    self.status_var_temp = 4
                    
                elif self.status_var_temp == 4:
                    # check if the vehicle is at the target position
                    if self.is_at_traj_setpoint(0.05):
                        self.get_logger().info("Vehicle is back at target position")                    
                        self.status_var_temp = 5
                        
                elif self.status_var_temp == 5:
                    self.get_logger().info("Vehicle is back at start position")
                    self.status_var_temp = 6
                
                            
            else:
                self.initialization_counter += 1
    
    def timer_debug_callback(self) -> None:
        if self.vehicle_status is None:
            return        

        # self.get_logger().info(f"Vehicle status: {self.vehicle_local_position}")  # used for development prints
        
        # Build the status report as a single multi-line string
        report = []
        report.append("\n=== Vehicle Status Report ===")
        
        # Arming Status
        report.append("--- Arming Status ---")
        arming_state = "ARMED ✅" if self.vehicle_status.arming_state == 2 else "DISARMED ❌"
        report.append(f"State: {arming_state}")
        
        if self.vehicle_status:
            current_time = self.vehicle_status.timestamp
        else: 
            current_time = 0
        current_time_formatted = datetime.fromtimestamp(current_time / 1e6).strftime('%d/%m/%Y %H:%M:%S')
        report.append(f"[s] Current time: {current_time_formatted}")
            
        if self.arm_timestamp:
            armed_duration = int(current_time - self.arm_timestamp) / 1e6  # Convert microseconds to seconds    
        else:
            armed_duration = 0
        report.append(f"[s] Armed time: {armed_duration:.0f}")
        
        if self.flight_start_timestamp:            
            flight_duration = int(current_time - self.flight_start_timestamp) / 1e6  # Convert microseconds to seconds        
        else:
            flight_duration = 0
        report.append(f"[s] Flight time: {flight_duration:.0f}")
        
        # Arming/disarming reasons
        arm_disarm_reasons = {
            0: "Transition to Standby", 1: "Stick Gesture", 2: "RC Switch",
            3: "Command Internal", 4: "Command External", 5: "Mission Start",
            6: "Safety Button", 7: "Auto Disarm Land", 8: "Auto Disarm Preflight",
            9: "Kill Switch", 10: "Lockdown", 11: "Failure Detector",
            12: "Shutdown", 13: "Unit Test"
        }
        
        if self.vehicle_status.arming_state == 1:
            reason = arm_disarm_reasons.get(self.vehicle_status.latest_disarming_reason, "Unknown")
            report.append(f"Last disarm reason: {reason}\n")
        elif self.vehicle_status.arming_state == 2:
            reason = arm_disarm_reasons.get(self.vehicle_status.latest_arming_reason, "Unknown")
            report.append(f"Last arm reason: {reason}\n")
            
        # Navigation State
        report.append("--- Navigation Status ---")
        navigation_states = {
            0: "Manual - Direct manual control via RC",
            1: "Altitude Control - Altitude stabilization",
            2: "Position Control - Position control with stabilization",
            3: "Auto Mission - Autonomous mission execution",
            4: "Auto Loiter - Holding position automatically",
            5: "Auto Return to Launch - Returning to launch position",
            10: "Acro - Raw rate control mode",
            12: "Descend - Controlled descent mode",
            13: "Termination - Emergency stop",
            14: "Offboard - External control mode",
            15: "Stabilized - Basic attitude stabilization",
            17: "Auto Takeoff - Automatic takeoff sequence",
            18: "Auto Land - Automatic landing sequence",
            19: "Auto Follow Target - Following a designated target",
            20: "Auto Precision Land - Precision landing mode",
            21: "Orbit - Orbital flight around a point",
            22: "Auto VTOL Takeoff - Vertical takeoff for VTOL aircraft"
        }
        navigation_state = navigation_states.get(self.vehicle_status.nav_state, f"Unknown ({self.vehicle_status.nav_state})")
        report.append(f"Current mode: {navigation_state}\n")
        
        # System Health
        report.append("--- System Health ---")
        if self.vehicle_status.failure_detector_status != 0:
            failures = []
            failures.append("FAILURES DETECTED:")
            if self.vehicle_status.failure_detector_status & 1:
                failures.append("- Roll failure")
            if self.vehicle_status.failure_detector_status & 2:
                failures.append("- Pitch failure")
            if self.vehicle_status.failure_detector_status & 4:
                failures.append("- Altitude failure")
            if self.vehicle_status.failure_detector_status & 8:
                failures.append("- External failure")
            if self.vehicle_status.failure_detector_status & 16:
                failures.append("- ARM ESC failure")
            if self.vehicle_status.failure_detector_status & 32:
                failures.append("- Battery failure")
            if self.vehicle_status.failure_detector_status & 64:
                failures.append("- Imbalanced prop")
            if self.vehicle_status.failure_detector_status & 128:
                failures.append("- Motor failure")
            report.append("\n".join(failures))
        else:
            report.append("No failures detected")
            
        # Add Battery Status
        if self.battery_status:
            report.append("\n--- Battery Status ---")
            remaining = self.battery_status.remaining * 100
            report.append(f"Voltage: {self.battery_status.voltage_v:.2f}V")
            report.append(f"Current: {self.battery_status.current_a:.2f}A")
            report.append(f"Remaining: {remaining:.1f}%")
            report.append(f"Consumed: {self.battery_status.discharged_mah:.0f}mAh")
            if self.battery_status.warning > 0:
                warnings = []
                if self.battery_status.warning >= 1:
                    warnings.append("LOW BATTERY WARNING ⚠️")
                if self.battery_status.warning >= 2:
                    warnings.append("CRITICAL BATTERY WARNING ❌")
                report.append("Warnings: " + ", ".join(warnings))
        
        # Communication Status
        report.append("\n--- Communication Status ---")
        gcs_emoji = "📡✅" if not self.vehicle_status.gcs_connection_lost else "📡❌"
        report.append(f"GCS Connection: {'LOST' if self.vehicle_status.gcs_connection_lost else 'OK'} {gcs_emoji}")
        if self.vehicle_status.gcs_connection_lost:
            report.append(f"Connection lost count: {self.vehicle_status.gcs_connection_lost_counter}")
        
        # Safety Status
        report.append("\n--- Safety Status ---")
        report.append(f"Pre-flight checks: {'PASS ✅' if self.vehicle_status.pre_flight_checks_pass else 'FAIL ❌'}")
        
        # Position Status
        report.append("\n--- Position Status ---")
        dead_reckoning_emoji = "🛰️❌" if self.vehicle_local_position.dead_reckoning else "🛰️✅"
        report.append(f"Dead reckoning: {self.vehicle_local_position.dead_reckoning} {dead_reckoning_emoji}")
        report.append("\nValidity Checks:")
        report.append(f"{'Sensor':20} {'Valid?':<10} {'Status'}")
        report.append("-" * 40)
        report.append(f"{'Terrain altitude':20} {str(self.vehicle_local_position.dist_bottom_valid):<10} {'✅' if self.vehicle_local_position.dist_bottom_valid else '❌'}")
        report.append(f"{'Position XY':20} {str(self.vehicle_local_position.xy_valid):<10} {'✅' if self.vehicle_local_position.xy_valid else '❌'}")
        report.append(f"{'Position Z':20} {str(self.vehicle_local_position.z_valid):<10} {'✅' if self.vehicle_local_position.z_valid else '❌'}")
        
        report.append("\nPosition Data:")
        report.append(f"{'Parameter':25} {'Value':>10} {'Uncertainty StdDev σ':>10}")
        report.append("-" * 45)
        report.append(f"{'[m] Terrain altitude':25} {self.vehicle_local_position.dist_bottom:>10.3f} {self.vehicle_local_position.dist_bottom_var:>10.3f}")
        report.append(f"{'[m] Local NED North':25} {self.vehicle_local_position.x:>10.3f} {self.vehicle_local_position.eph:>10.3f}")
        report.append(f"{'[m] Local NED East':25} {self.vehicle_local_position.y:>10.3f} {self.vehicle_local_position.eph:>10.3f}")
        report.append(f"{'[m] Local NED Z':25} {self.vehicle_local_position.z:>10.3f} {self.vehicle_local_position.epv:>10.3f}")
        
        report.append("\nGlobal Position:")
        report.append(f"{'Parameter':20} {'Valid?':<8} {'Value'}")
        report.append("-" * 40)
        report.append(f"{'Global XY':20} {str(self.vehicle_local_position.xy_global):<8} {self.vehicle_local_position.ref_lat:>.3f}, {self.vehicle_local_position.ref_lon:.3f}")
        report.append(f"{'Global Z':20} {str(self.vehicle_local_position.z_global):<8} {self.vehicle_local_position.ref_alt:.3f}")
        
        report.append("\nVelocity Data:")
        report.append(f"{'Parameter':25} {'Value':>10} {'Uncertainty Std Dev σ':>10}")
        report.append("-" * 45)
        report.append(f"{'[m/s] Local NED VNorth':25} {self.vehicle_local_position.vx:>10.3f} {self.vehicle_local_position.evh:>10.3f}")
        report.append(f"{'[m/s] Local NED VEast':25} {self.vehicle_local_position.vy:>10.3f} {self.vehicle_local_position.evh:>10.3f}")
        report.append(f"{'[m/s] Local NED VZ':25} {self.vehicle_local_position.vz:>10.3f} {self.vehicle_local_position.evv:>10.3f}")
        
        readable_heading = self.px4_yaw_to_heading(self.vehicle_local_position.heading)
        report.append(f"\n{'[deg] Global Heading':25} {readable_heading:>10.3f} {self.vehicle_local_position.heading_var * 57.2958:>10.3f}")
        
        self.get_logger().info("\n".join(report))

    
    # -----


    def publish_offboard_control_heartbeat_signal(self):
        """Publish the offboard control mode."""
        msg = OffboardControlMode()
        msg.position = True
        msg.velocity = False
        msg.acceleration = False
        msg.attitude = False
        msg.body_rate = False
        msg.timestamp = int(self.get_clock().now().nanoseconds / 1000)
        self.offboard_control_mode_publisher.publish(msg)
        
    def publish_vehicle_command(self, command, **params) -> None:
        """Publish a vehicle command."""
        msg = VehicleCommand()
        msg.command = command
        msg.param1 = params.get("param1", 0.0)
        msg.param2 = params.get("param2", 0.0)
        msg.param3 = params.get("param3", 0.0)
        msg.param4 = params.get("param4", 0.0)
        msg.param5 = params.get("param5", 0.0)
        msg.param6 = params.get("param6", 0.0)
        msg.param7 = params.get("param7", 0.0)
        msg.target_system = 1
        msg.target_component = 1
        msg.source_system = 1
        msg.source_component = 1
        msg.from_external = True
        msg.timestamp = int(self.get_clock().now().nanoseconds / 1000)
        self.vehicle_command_publisher.publish(msg)
        
    def publish_trajectory_setpoint(self, position: list[float, float, float], yaw:float = None) -> None:
        """Publish a trajectory setpoint in drone NED with home offset applied
        Takes in a position list [x, y, z] in meters and a yaw in degrees as bearing"""
        msg = TrajectorySetpoint()
        msg.position = [position[0] + self.home_coord_offset[0], position[1] + self.home_coord_offset[1], position[2] + self.home_coord_offset[2]]
        if yaw is not None:
            msg.yaw = yaw * (math.pi / 180.0)  # Convert degrees to radians
        else:
            msg.yaw = self.vehicle_local_position.heading
        msg.timestamp = int(self.get_clock().now().nanoseconds / 1000)
        self.last_traj_setpoint_msg = msg
        self.get_logger().info(f"Trajectory setpoint: {msg.position}")
        # self.trajectory_setpoint_publisher.publish(msg)
        
    # -----
    
    def heading_to_px4_yaw(self, heading: float) -> float:
        """Convert a heading in degrees [0, 360) to a PX4 yaw in radians (-pi, pi]
        Heading is in degrees, yaw is in radians"""
        heading = heading % 360.0
        if heading > 180.0:
            heading = heading - 360.0
        return heading * (math.pi / 180.0) # heading is backwards 
    
    def px4_yaw_to_heading(self, yaw: float) -> float:
        """Convert a PX4 yaw in radians (-pi, pi] to a heading in degrees [0, 360)
        Yaw is in radians, heading is in degrees"""
        return (yaw * (180.0 / math.pi)) % 360.0
    
    # take in a local offset in meters and a yaw in degrees, convert to the NED frame, and return the NED coordinates
    def ned_point_from_flu_offset(self, curr_ned_pos: list[float, float, float], offset_flu: list[float, float, float]) -> list[float, float, float]:
        """Convert a local FLU offset in meters to NED coordinates
        Returns the offset in NED, not the global NED"""
        # convert yaw to radians
        yaw_current = self.vehicle_local_position.heading    
        
        # convert the offset to rotated FLU
        rotated_flu_x = offset_flu[0] * math.cos(yaw_current) + offset_flu[1] * math.sin(yaw_current)
        rotated_flu_y = offset_flu[0] * math.sin(yaw_current) - offset_flu[1] * math.cos(yaw_current)
        rotated_flu_z = offset_flu[2]
        
        # convert to NED coordinates
        offset_ned = [rotated_flu_x, rotated_flu_y, -rotated_flu_z]  
        return [curr_ned_pos[0] + offset_ned[0], curr_ned_pos[1] + offset_ned[1], curr_ned_pos[2] + offset_ned[2]]
    
    def check_terrain_safe(self, threshold=0.5) -> bool:
        """Check if the terrain is clear
        Threshold is the minimum distance to the terrain in meters
        Returns true if the terrain is safe, false otherwise"""
        if self.vehicle_local_position.dist_bottom > threshold and self.vehicle_local_position.dist_bottom_valid:
            return True
        else:
            return False
        
        
    # !! TODO: needs checks with the home_coord_offset and the logic to make it work w/o having to add/subtract the offset all the time
    def get_current_ned_pos(self) -> list[float, float, float]:
        """Get the current position in NED coordinates
        Returns the current position in NED coordinates"""
        return [self.vehicle_local_position.x - self.home_coord_offset[0], self.vehicle_local_position.y - self.home_coord_offset[1], self.vehicle_local_position.z - self.home_coord_offset[2]]
    
    def get_traj_setpoint(self) -> list[float, float, float]:
        """Get the trajectory setpoint in NED coordinates
        Returns the trajectory setpoint in NED coordinates"""
        if self.last_traj_setpoint_msg is None:
            return None
        return self.get_ned_pos(self.last_traj_setpoint_msg.position)
    
    def get_ned_pos(self, target: list[float, float, float]) -> list[float, float, float]:
        """Get the position in NED coordinates
        Returns the position in NED coordinates"""
        return [target[0] - self.home_coord_offset[0], target[1] - self.home_coord_offset[1], target[2] - self.home_coord_offset[2]]
    
    def is_at_position(self, target: list[float, float, float], threshold=0.5) -> bool:
        """Check if the vehicle is at a given position, threshold is in meters
        Returns true if the vehicle is at the position, false otherwise"""
        curr_pos = self.get_current_ned_pos()
        if math.sqrt((curr_pos[0] - target[0])**2 + (curr_pos[1] - target[1])**2 + (curr_pos[2] - target[2])**2) < threshold:
            return True
        else:
            return False
        
    def is_at_traj_setpoint(self, threshold=0.5) -> bool:
        """Check if the vehicle is at the trajectory setpoint
        Threshold is in meters
        Returns true if the vehicle is at the trajectory setpoint, false otherwise"""
        if self.last_traj_setpoint_msg is None:
            return False
        return self.is_at_position(self.get_traj_setpoint(), threshold)
    
    # -----
                
    def arm(self):
        """Send an arm command to the vehicle."""
        self.publish_vehicle_command(
            VehicleCommand.VEHICLE_CMD_COMPONENT_ARM_DISARM, param1=1.0)
        self.get_logger().info('Arm command sent')

    def disarm(self):
        """Send a disarm command to the vehicle."""
        self.publish_vehicle_command(
            VehicleCommand.VEHICLE_CMD_COMPONENT_ARM_DISARM, param1=0.0)
        self.get_logger().info('Disarm command sent')
        
    def land(self):
        """Switch to land mode."""
        self.publish_vehicle_command(VehicleCommand.VEHICLE_CMD_NAV_LAND)
        self.get_logger().info("Switching to land mode")
        
    # -----
        

def main(args=None) -> None:
    print("Starting vehicle_global_position listener node...")
    rclpy.init(args=args)
    vehicle_global_position_listener = VehicleGlobalPositionListener()
    rclpy.spin(vehicle_global_position_listener)
    
    vehicle_global_position_listener.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(e)