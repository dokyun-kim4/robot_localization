#!/usr/bin/env python3

""" This is the starter code for the robot localization project """
import rclpy
from threading import Thread
from rclpy.time import Time
from rclpy.node import Node
from std_msgs.msg import Header
from sensor_msgs.msg import LaserScan
from nav2_msgs.msg import ParticleCloud, Particle
from nav2_msgs.msg import Particle as Nav2Particle
from geometry_msgs.msg import PoseWithCovarianceStamped, Pose, Point, Quaternion
from rclpy.duration import Duration
import math
import time
import numpy as np
from occupancy_field import OccupancyField
from helper_functions import TFHelper, draw_random_sample
from rclpy.qos import qos_profile_sensor_data
from angle_helpers import quaternion_from_euler
import statistics as st

class Particle(object):
    """ Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized
    """

    def __init__(self, x=0.0, y=0.0, theta=0.0, w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of KeyboardInterruptthe hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized """ 
        self.w = w
        self.theta = theta
        self.x = x
        self.y = y

    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose message """
        q = quaternion_from_euler(0, 0, self.theta)
        return Pose(position=Point(x=self.x, y=self.y, z=0.0),
                    orientation=Quaternion(x=q[0], y=q[1], z=q[2], w=q[3]))

    # TODO: define additional helper functions if needed

class ParticleFilter(Node):
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            base_frame: the name of the robot base coordinate frame (should be "base_footprint" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            last_scan_timestamp: this is used to keep track of the clock when using bags
            scan_to_process: the scan that our run_loop should process next
            occupancy_field: this helper class allows you to query the map for distance to closest obstacle
            transform_helper: this helps with various transform operations (abstracting away the tf2 module)
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            thread: this thread runs your main loop
    """
    def __init__(self):
        super().__init__('pf')
        self.base_frame = "base_footprint"   # the frame of the robot base
        self.map_frame = "map"          # the name of the map coordinate frame
        self.odom_frame = "odom"        # the name of the odometry coordinate frame
        self.scan_topic = "scan"        # the topic where we will get laser scans from 

        self.n_particles = 500          # the number of particles to use


        self.d_thresh = 0.1             # the amount of linear movement before performing an update
        self.a_thresh = math.pi/6       # the amount of angular movement before performing an update

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        self.create_subscription(PoseWithCovarianceStamped, 'initialpose', self.update_initial_pose, 10)

        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.particle_pub = self.create_publisher(ParticleCloud, "particle_cloud", qos_profile_sensor_data)

        # laser_subscriber listens for data from the lidar
        self.create_subscription(LaserScan, self.scan_topic, self.scan_received, 10)
        self.min_dist = 0 

        # this is used to keep track of the timestamps coming from bag files
        # knowing this information helps us set the timestamp of our map -> odom
        # transform correctly
        self.last_scan_timestamp = None
        # this is the current scan that our run_loop should process
        self.scan_to_process = None
        # your particle cloud will go here
        self.particle_cloud = []

        self.current_odom_xy_theta = []
        self.occupancy_field = OccupancyField(self)
        self.transform_helper = TFHelper(self)

        # we are using a thread to work around single threaded execution bottleneck
        thread = Thread(target=self.loop_wrapper)
        thread.start()
        self.transform_update_timer = self.create_timer(0.05, self.pub_latest_transform)

    def pub_latest_transform(self):
        """ This function takes care of sending out the map to odom transform """
        if self.last_scan_timestamp is None:
            return
        postdated_timestamp = Time.from_msg(self.last_scan_timestamp) + Duration(seconds=0.1)
        self.transform_helper.send_last_map_to_odom_transform(self.map_frame, self.odom_frame, postdated_timestamp)

    def loop_wrapper(self):
        """ This function takes care of calling the run_loop function repeatedly.
            We are using a separate thread to run the loop_wrapper to work around
            issues with single threaded executors in ROS2 """
        while True:
            self.run_loop()
            time.sleep(0.1)

    def run_loop(self):
        """ This is the main run_loop of our particle filter.  It checks to see if
            any scans are ready and to be processed and will call several helper
            functions to complete the processing.
            
            You do not need to modify this function, but it is helpful to understand it.
        """
        if self.scan_to_process is None:
            return
        msg = self.scan_to_process

        (new_pose, delta_t) = self.transform_helper.get_matching_odom_pose(self.odom_frame,
                                                                           self.base_frame,
                                                                           msg.header.stamp)
        if new_pose is None:
            # we were unable to get the pose of the robot corresponding to the scan timestamp
            if delta_t is not None and delta_t < Duration(seconds=0.0):
                # we will never get this transform, since it is before our oldest one
                self.scan_to_process = None
            return
        
        (r, theta) = self.transform_helper.convert_scan_to_polar_in_robot_frame(msg, self.base_frame)
        #print("r[0]={0}, theta[0]={1}".format(r[0], theta[0]))
        # clear the current scan so that we can process the next one
        self.scan_to_process = None

        self.odom_pose = new_pose
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose)
        # print("x: {0}, y: {1}, yaw: {2}".format(*new_odom_xy_theta))
        if not self.current_odom_xy_theta:
            self.current_odom_xy_theta = new_odom_xy_theta
        elif not self.particle_cloud:
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud(msg.header.stamp)
        elif self.moved_far_enough_to_update(new_odom_xy_theta):
            # we have moved far enough to do an update!
            self.update_particles_with_odom()    # update based on odometry
            self.update_particles_with_laser(r, theta)   # update based on laser scan
            self.update_robot_pose()                # update robot's pose based on particles
            self.resample_particles()               # resample particles to focus on areas of high density
        # publish particles (so things like rviz can see them)
        self.publish_particles(msg.header.stamp)



    def moved_far_enough_to_update(self, new_odom_xy_theta):
        return math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or \
               math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or \
               math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh


    def update_robot_pose(self): # TUNING NEEDED
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose
                (2): compute the most likely pose (i.e. the mode of the distribution)
        """
        self.normalize_particles()
        # Isolate particles that are above a certain weight threshold
        filtered = []
        weight_thresh = 0.007
        for particle in self.particle_cloud:
            if particle.w >= weight_thresh:
                filtered.append(particle)
        print(f"{len(filtered)} particles in filtered list")
        
        if filtered:
            # Using MEAN
            new_x = st.mean([particle.x for particle in filtered])
            new_y = st.mean([particle.y for particle in filtered])
            new_t = st.mean([particle.theta for particle in filtered])

            q = quaternion_from_euler(0,0,new_t)
            self.robot_pose = Pose(position = Point(x=new_x,y=new_y,z=0.0),
                                orientation=Quaternion(x=q[0],y=q[1],z=q[2],w=q[3]))
        else:
            self.robot_pose = Pose()
            print(f"{filtered}, robot sent to origin")
        if hasattr(self, 'odom_pose'):
            self.transform_helper.fix_map_to_odom_transform(self.robot_pose,
                                                            self.odom_pose)
        else:
            self.get_logger().warn("Can't set map->odom transform since no odom data received")

    def update_particles_with_odom(self): # WORKS
        """ Update the particles using the newly given odometry pose.
            The function computes the value delta which is a tuple (x,y,theta)
            that indicates the change in position and angle between the odometry
            when the particles were last updated and the current odometry.
        """
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose)
        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            old_odom_xy_theta = self.current_odom_xy_theta

            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return

        old_odom_trans = np.array([[np.cos(old_odom_xy_theta[2]),-1*np.sin(old_odom_xy_theta[2]),old_odom_xy_theta[0]],
                              [np.sin(old_odom_xy_theta[2]),np.cos(old_odom_xy_theta[2]),old_odom_xy_theta[1]],
                              [0,0,1]])
        new_odom_trans = np.array([[np.cos(new_odom_xy_theta[2]),-1*np.sin(new_odom_xy_theta[2]),new_odom_xy_theta[0]],
                              [np.sin(new_odom_xy_theta[2]),np.cos(new_odom_xy_theta[2]),new_odom_xy_theta[1]],
                              [0,0,1]])
        trans_mat = np.linalg.inv(old_odom_trans) @ new_odom_trans

        for particle in self.particle_cloud:
            particle_trans = np.array([[np.cos(particle.theta),-1*np.sin(particle.theta),particle.x],
                              [np.sin(particle.theta),np.cos(particle.theta),particle.y],
                              [0,0,1]])
            updated_particle_trans = particle_trans @ trans_mat @ np.linalg.inv(particle_trans)

            coord = np.array([particle.x,
                              particle.y,
                              1.0])
            
            new_coord = updated_particle_trans @ coord
            particle.x = new_coord[0]
            particle.y = new_coord[1]
            particle.theta += math.atan2(trans_mat[1][0], trans_mat[0][0])
                 

    def resample_particles(self): # TUNING NEEDED
        """ Resample the particles according to the new particle weights.
            The weights stored with each particle should define the probability that a particular
            particle is selected in the resampling step.  You may want to make use of the given helper
            function draw_random_sample in helper_functions.py.
        """
        # TOP `n` PARTICLES
        n = 3
        sorted_particles = sorted(self.particle_cloud, key=lambda particle: particle.w, reverse=True)[:n]
        print([p.w for p in sorted_particles])
        # Reset particle cloud
        self.particle_cloud = []
        for particle in sorted_particles:
            xc,yc = particle.x,particle.y
            mu = [xc,yc]
            sigma = 0.15
            x_coords,y_coords = np.random.multivariate_normal(mu, [[sigma,0],[0,sigma]], (self.n_particles-n)//n).T

            # Add exponential noise to angle
            angle_noise = np.random.exponential(40,self.n_particles-1)
            angle_with_noise = [np.deg2rad(particle.theta + noise) for noise in angle_noise]
            
            for i in range((self.n_particles-n)//n):
                x = x_coords[i]
                y = y_coords[i]
                theta = angle_with_noise[i]
                self.particle_cloud.append(Particle(x=x,y=y,theta=theta,w=1.0))


    def update_particles_with_laser(self, r, theta):
        """ Updates the particle weights in response to the scan data
            r: the distance readings to obstacles
            theta: the angle relative to the robot frame for each corresponding reading 
        """
        # If within 3cm, the particle is significant
        laser_dist_thresh = 0.03

        # percent based weighting
        for particle in self.particle_cloud:
            weight_sum = 0
            for i in range(len(r)):
                phi = particle.theta
                proj_laserscan_x = particle.x+r[i]*np.cos(phi + theta[i])
                proj_laserscan_y = particle.y+r[i]*np.sin(phi + theta[i])
                if proj_laserscan_x in [float('-inf'), float('inf')] or proj_laserscan_y in [float('-inf'), float('inf')]:
                    pass
                else:
                    dist_from_obstacle = self.occupancy_field.get_closest_obstacle_distance(proj_laserscan_x, proj_laserscan_y)
                    if dist_from_obstacle < laser_dist_thresh:
                        weight = abs(laser_dist_thresh - dist_from_obstacle) / laser_dist_thresh
                        weight_sum += weight
            particle.w = weight_sum
    

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(msg.pose.pose)
        self.initialize_particle_cloud(msg.header.stamp, xy_theta)

    def initialize_particle_cloud(self, timestamp, xy_theta=None): # WORKS
        """ Initialize the particle cloud.
            Arguments
            xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
                      particle cloud around.  If this input is omitted, the odometry will be used """
        if xy_theta is None:
            xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose)
        self.particle_cloud = []

        # Use 2D gaussian to initialize particles 
        xc,yc = xy_theta[0],xy_theta[1]
        mu = [xc,yc]
        sigma = 0.15
        x_coords,y_coords = np.random.multivariate_normal(mu, [[sigma,0],[0,sigma]], self.n_particles).T
        for i in range(self.n_particles):
            x = x_coords[i]
            y = y_coords[i]
            theta = np.deg2rad(np.random.uniform(low=0,high=359,size=(1))[0])
            self.particle_cloud.append(Particle(x=x,y=y,theta=theta,w=1.0))

        self.normalize_particles()
        self.update_robot_pose()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e. sum to 1.0) """
        weight_sum = np.sum(np.array([particle.w for particle in self.particle_cloud]))
        for particle in self.particle_cloud:
            particle.w /= weight_sum

    def publish_particles(self, timestamp):
        msg = ParticleCloud()
        msg.header.frame_id = self.map_frame
        msg.header.stamp = timestamp
        for p in self.particle_cloud:
            msg.particles.append(Nav2Particle(pose=p.as_pose(), weight=p.w))
        self.particle_pub.publish(msg)

    def scan_received(self, msg: LaserScan):
        self.last_scan_timestamp = msg.header.stamp
        # we throw away scans until we are done processing the previous scan
        # self.scan_to_process is set to None in the run_loop 
        if self.scan_to_process is None:
            self.scan_to_process = msg
        
        self.min_dist = msg.range_min

def main(args=None):
    rclpy.init()
    n = ParticleFilter()
    rclpy.spin(n)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
