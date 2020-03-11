import math
import numpy as np
import random
import gym
from gym import spaces
# from gym.utils import seeding
from collections import OrderedDict
# from shapely.geometry import Polygon, Point
import matplotlib.path as mpltPath

from config_hex_sec import Config

__author__ = "Xuxi Yang <xuxiyang@iastate.edu>"

import ipdb


class MultiAircraftEnv(gym.Env):
    """
    This is the airspace simulator where we can control multiple aircraft to their respective
    goal position while avoiding conflicts between each other.
    **STATE:**
    The state consists all the information needed for the aircraft to choose an optimal action:
    position, velocity, speed, heading, goal position, of each aircraft.
    In the beginning of each episode, all the aircraft and their goals are initialized randomly.
    **ACTIONS:**
    The action is either applying +1, 0 or -1 for the change of heading angle of each aircraft.
    """

    def __init__(self, sd, debug=False):
        self.load_config()  # read parameters from config file
        self.load_vertiports()  # set vertiports
        self.load_sectors()  # set sectors
        self.state = None
        self.viewer = None

        # build observation space and action space
        self.observation_space = self.build_observation_space()
        self.position_range = spaces.Box(
            low=np.array([0, 0]),
            high=np.array([self.window_width, self.window_height]),
            dtype=np.float32)
        self.action_space = spaces.Tuple((spaces.Discrete(3),) * self.num_aircraft)

        self.total_timesteps = 0  # total time steps in seconds

        self.conflicts = 0  # number of conflicts (LOS)
        self.seed(sd)  # set seed

        self.debug = debug

    def seed(self, seed=None):
        np.random.seed(seed)
        random.seed(seed)
        # self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def load_config(self):
        # load parameters from config file
        self.window_width = Config.window_width
        self.window_height = Config.window_height
        self.num_aircraft = Config.num_aircraft
        self.EPISODES = Config.EPISODES
        self.G = Config.G
        self.tick = Config.tick
        self.scale = Config.scale  # scale: 1 pixel = ? meters in real world
        self.minimum_separation = Config.minimum_separation  # LOS standard: 0.3 nmi
        self.NMAC_dist = Config.NMAC_dist  # NMAC standard: 500 ft
        self.horizon_dist = Config.horizon_dist
        self.initial_min_dist = Config.initial_min_dist  # newly added aircraft should not be too close to existing aircraft
        self.goal_radius = Config.goal_radius
        self.init_speed = Config.init_speed  # cruise speed of the aircraft
        self.min_speed = Config.min_speed
        self.max_speed = Config.max_speed

    def load_vertiports(self):
        # load vertiports based on locations in config file
        self.vertiport_list = []
        for i in range(Config.vertiport_loc.shape[0]):
            self.vertiport_list.append(VertiPort(id=i, position=Config.vertiport_loc[i]))

    def load_sectors(self):
        # load sectors based on locations in config file
        self.sectors = []
        for i in range(7):
            self.sectors.append(Sector(i, Config.sector_vertices[i]))

    def reset(self):
        self.aircraft_dict = AircraftDict()
        self.id_tracker = 0

        self.conflicts = 0
        self.goals = 0
        self.NMACs = 0

        return self._get_ob()

    def _get_ob(self):
        ob = {}  # dict: {sector_id: [aircraft_info, id]}
        for i in range(7):  # loop over all sectors
            s = []
            id = []
            goal_exit_id = []
            for aircraft_id in self.sectors[i].controlled_aircraft_id:
                # (x, y, vx, vy, speed, heading, gx, gy)
                aircraft = self.aircraft_dict.get_aircraft_by_id(aircraft_id)
                s.append(aircraft.position[0])
                s.append(aircraft.position[1])
                s.append(aircraft.velocity[0])
                s.append(aircraft.velocity[1])
                s.append(aircraft.speed)
                s.append(aircraft.heading)
                s.append(aircraft.sub_goal.position[0])
                s.append(aircraft.sub_goal.position[1])

                id.append(aircraft_id)

                goal_exit_id.append(aircraft.goal_exit_id)

            current_sector = self.sectors[i]
            # add aircraft information close to current sector
            for sector in self.sectors:
                if not sector.id == i:
                    for aircraft_id in sector.controlled_aircraft_id:
                        pos = self.aircraft_dict.get_aircraft_by_id(aircraft_id).position
                        dist_segment_list = [
                            pnt2line(pos, current_sector.vertices[k], current_sector.vertices[k + 1])[0]
                            for k in range(-1, len(current_sector.vertices) - 1)]
                        if min(dist_segment_list) < 3 * Config.minimum_separation:
                            aircraft = self.aircraft_dict.get_aircraft_by_id(aircraft_id)
                            s.append(aircraft.position[0])
                            s.append(aircraft.position[1])
                            s.append(aircraft.velocity[0])
                            s.append(aircraft.velocity[1])
                            s.append(aircraft.speed)
                            s.append(aircraft.heading)
                            s.append(aircraft.sub_goal.position[0])
                            s.append(aircraft.sub_goal.position[1])

            ob[i] = [np.reshape(s, (-1, 8)), id, goal_exit_id]

        return ob

    def step(self, a, near_end=False):
        # a is a dictionary: {id: action, ...}
        for id, aircraft in self.aircraft_dict.ac_dict.items():
            try:
                aircraft.step(a[id])
            except KeyError:
                aircraft.step()

        for vertiport in self.vertiport_list:
            vertiport.step()  # add the vertiport clock by 1
            if vertiport.clock_counter >= vertiport.time_next_aircraft and not near_end:
                goal_vertiport_id = random.choice([e for e in range(len(self.vertiport_list)) if not e == vertiport.id])
                aircraft = Aircraft(
                    id=self.id_tracker,
                    position=vertiport.position,
                    speed=self.init_speed,
                    heading=self.random_heading(),
                    goal_pos=self.vertiport_list[goal_vertiport_id].position,
                    goal_vertiport_id=goal_vertiport_id,
                    sector_id=-1
                )
                dist_array, id_array = self.dist_to_all_aircraft(aircraft)
                min_dist = min(dist_array) if dist_array.shape[0] > 0 else 9999
                if min_dist > Config.start_safe_dist:
                    # add aircraft only when it is safe
                    self.aircraft_dict.add(aircraft)
                    self.id_tracker += 1

                    vertiport.generate_interval()

        reward, terminal, info = self._terminal_reward()

        self.assign_sector()

        self.total_timesteps += self.aircraft_dict.num_aircraft

        return self._get_ob(), reward, terminal, info

    def assign_sector(self):
        """
        based on the aircraft location, change its sector id to the current sector
        """
        for id, aircraft in self.aircraft_dict.ac_dict.items():
            for sector in self.sectors:
                if sector.in_sector(aircraft.position):
                    if not aircraft.sector_id == sector.id:
                        self.sectors[aircraft.sector_id].controlled_aircraft_id.discard(id)
                        if not aircraft.sector_id == -1:
                            self.sectors[aircraft.sector_id].exited_aircraft_id[id] = 0
                        aircraft.sector_id = sector.id
                        sector.controlled_aircraft_id.add(id)
                        sector.assign_exit(aircraft)

                    break

    def _terminal_reward(self):
        """
        determine the reward and terminal for the current transition, and use info. Main idea:
        1. for each aircraft:
          a. if there is no_conflict, return a large penalty and terminate
          b. elif it is out of map, assign its reward as self.out_of_map_penalty, prepare to remove it
          c. elif if it reaches goal, assign its reward as simulator, prepare to remove it
          d. else assign its reward as simulator
        2. accumulates the reward for all aircraft
        3. remove out-of-map aircraft and goal-aircraft
        4. if all aircraft are removed, return reward and terminate
           else return the corresponding reward and not terminate
        """
        reward = 0
        info_dist_dict = {}
        aircraft_to_remove = []  # add goal-aircraft and out-of-map aircraft to this list

        for id, aircraft in self.aircraft_dict.ac_dict.items():
            # calculate min_dist and dist_goal for checking terminal
            dist_array, id_array = self.dist_to_all_aircraft(aircraft)
            min_dist = min(dist_array) if dist_array.shape[0] > 0 else 9999
            info_dist_dict[id] = min_dist
            dist_goal = self.dist_goal(aircraft)

            conflict = False
            # set the conflict flag to false for aircraft
            # elif conflict, set penalty reward and conflict flag but do NOT remove the aircraft from list
            for id2, dist in zip(id_array, dist_array):
                if dist >= self.minimum_separation:  # safe
                    aircraft.conflict_id_set.discard(id2)  # discarding element not in the set won't raise error

                else:  # conflict!!
                    if self.debug:
                        self.render()
                        import ipdb
                        ipdb.set_trace()
                    conflict = True
                    if id2 not in aircraft.conflict_id_set:
                        self.conflicts += 1
                        aircraft.conflict_id_set.add(id2)
                    aircraft.reward = Config.conflict_penalty

            # if NMAC, set penalty reward and prepare to remove the aircraft from list
            if min_dist < self.NMAC_dist:
                if self.debug:
                    self.render()
                    import ipdb
                    ipdb.set_trace()
                aircraft.reward = Config.NMAC_penalty
                aircraft_to_remove.append(aircraft)
                self.NMACs += 1
                # aircraft_to_remove.append(self.aircraft_dict.get_aircraft_by_id(close_id))

            # give out-of-map aircraft a penalty, and prepare to remove it
            # elif not self.position_range.contains(np.array(aircraft.position)):
            #     aircraft.reward = Config.wall_penalty
            #     # info['w'].append(aircraft.id)
            #     if aircraft not in aircraft_to_remove:
            #         aircraft_to_remove.append(aircraft)

            # set goal-aircraft reward according to simulator, prepare to remove it
            elif dist_goal < self.goal_radius:
                aircraft.reward = Config.goal_reward
                self.goals += 1
                if aircraft not in aircraft_to_remove:
                    aircraft_to_remove.append(aircraft)

            # for aircraft without NMAC, conflict, out-of-map, goal, set its reward as simulator
            elif not conflict:
                aircraft.reward = Config.step_penalty

            # accumulates reward
            reward += aircraft.reward

        # remove all the out-of-map aircraft and goal-aircraft
        for aircraft in aircraft_to_remove:
            self.sectors[aircraft.sector_id].controlled_aircraft_id.discard(aircraft.id)
            self.aircraft_dict.remove(aircraft)
        # reward = [e.reward for e in self.aircraft_dict]

        return reward, False, info_dist_dict

    def render(self, mode='human'):
        from gym.envs.classic_control import rendering
        from colour import Color
        red = Color('red')
        colors = list(red.range_to(Color('green'), self.num_aircraft))

        if self.viewer is None:
            self.viewer = rendering.Viewer(self.window_width, self.window_height)
            self.viewer.set_bounds(0, self.window_width, 0, self.window_height)

        import os
        __location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))

        # vertiport_map_img = rendering.Image(os.path.join(__location__, 'images/vertiport_map.png'), 800, 800)
        # jtransform = rendering.Transform(rotation=0, translation=[400, 400])
        # vertiport_map_img.add_attr(jtransform)
        # self.viewer.onetime_geoms.append(vertiport_map_img)

        # draw all aircraft
        for id, aircraft in self.aircraft_dict.ac_dict.items():
            aircraft_img = rendering.Image(os.path.join(__location__, 'images/aircraft.png'), 32, 32)
            jtransform = rendering.Transform(rotation=aircraft.heading - math.pi / 2, translation=aircraft.position)
            aircraft_img.add_attr(jtransform)
            r, g, b = colors[aircraft.id % self.num_aircraft].get_rgb()
            aircraft_img.set_color(r, g, b)
            self.viewer.onetime_geoms.append(aircraft_img)

            goal_img = rendering.Image(os.path.join(__location__, 'images/goal.png'), 32, 32)
            jtransform = rendering.Transform(rotation=0, translation=aircraft.goal.position)
            goal_img.add_attr(jtransform)
            goal_img.set_color(r, g, b)
            self.viewer.onetime_geoms.append(goal_img)

        # draw all vertiports
        for veriport in self.vertiport_list:
            vertiport_img = rendering.Image(os.path.join(__location__, 'images/verti.png'), 32, 32)
            jtransform = rendering.Transform(rotation=0, translation=veriport.position)
            vertiport_img.add_attr(jtransform)
            self.viewer.onetime_geoms.append(vertiport_img)

        # draw all sectors
        for sector in self.sectors:
            if sector.id == 0:
                for i, exit in enumerate(sector.exits):
                    exit_img = self.viewer.draw_polygon(Config.vertices)
                    jtransform = rendering.Transform(rotation=math.radians(60 * i - 90), translation=exit[0])
                    exit_img.add_attr(jtransform)
                    exit_img.set_color(255 / 255.0, 165 / 255.0, 0)
                    self.viewer.onetime_geoms.append(exit_img)

            else:
                for i, exit in enumerate(sector.exits):
                    exit_img = self.viewer.draw_polygon(Config.vertices)
                    angle = 60 * (sector.id + i) + 30
                    jtransform = rendering.Transform(rotation=math.radians(angle), translation=exit[0])
                    exit_img.add_attr(jtransform)
                    exit_img.set_color(255 / 255.0, 165 / 255.0, 0)
                    self.viewer.onetime_geoms.append(exit_img)

        # draw boundaries of the sectors
        self.viewer.draw_polyline(Config.vertiport_loc[[1, 2, 3, 4, 5, 6, 1], :])
        for sector in self.sectors:
            self.viewer.draw_polyline(sector.vertices[[0, 1, 2, 3, 4, 5, 0], :])

        return self.viewer.render(return_rgb_array=False)

    def draw_point(self, point):
        # for debug
        from gym.envs.classic_control import rendering

        if self.viewer is None:
            self.viewer = rendering.Viewer(self.window_width, self.window_height)
            self.viewer.set_bounds(0, self.window_width, 0, self.window_height)

        img = self.viewer.draw_polygon(Config.point)
        jtransform = rendering.Transform(rotation=0, translation=point)
        img.add_attr(jtransform)
        self.viewer.onetime_geoms.append(img)

        import os
        __location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))

        for veriport in self.vertiport_list:
            vertiport_img = rendering.Image(os.path.join(__location__, 'images/verti.png'), 32, 32)
            jtransform = rendering.Transform(rotation=0, translation=veriport.position)
            vertiport_img.add_attr(jtransform)
            self.viewer.onetime_geoms.append(vertiport_img)

        for sector in self.sectors:
            if sector.id == 0:
                for i, exit in enumerate(sector.exits):
                    exit_img = self.viewer.draw_polygon(Config.vertices)
                    jtransform = rendering.Transform(rotation=math.radians(60 * i - 90), translation=exit[0])
                    exit_img.add_attr(jtransform)
                    exit_img.set_color(255 / 255.0, 165 / 255.0, 0)
                    self.viewer.onetime_geoms.append(exit_img)

            else:
                for i, exit in enumerate(sector.exits):
                    exit_img = self.viewer.draw_polygon(Config.vertices)
                    angle = 60 * (sector.id + i) + 30
                    jtransform = rendering.Transform(rotation=math.radians(angle), translation=exit[0])
                    exit_img.add_attr(jtransform)
                    exit_img.set_color(255 / 255.0, 165 / 255.0, 0)
                    self.viewer.onetime_geoms.append(exit_img)

        self.viewer.draw_polyline(Config.vertiport_loc[[1, 2, 3, 4, 5, 6, 1], :])
        for sector in self.sectors:
            self.viewer.draw_polyline(sector.vertices[[0, 1, 2, 3, 4, 5, 0], :])

        return self.viewer.render(return_rgb_array=False)

    def close(self):
        if self.viewer:
            self.viewer.close()
            self.viewer = None

    def dist_to_all_aircraft(self, aircraft):
        # calculate dist of aircraft to all of the other aircraft
        id_list = []
        dist_list = []
        for id, intruder in self.aircraft_dict.ac_dict.items():
            if id != aircraft.id:
                id_list.append(id)
                dist_list.append(self.metric(aircraft.position, intruder.position))

        return np.array(dist_list), np.array(id_list)

    def dist_goal(self, aircraft):
        return self.metric(aircraft.position, aircraft.goal.position)

    def metric(self, pos1, pos2):
        # the distance between two points
        dx = pos1[0] - pos2[0]
        dy = pos1[1] - pos2[1]
        return math.sqrt(dx ** 2 + dy ** 2)

    # def dist(self, pos1, pos2):
    #     return np.linalg.norm(np.array(pos1) - np.array(pos2))

    def random_pos(self):
        return np.random.uniform(
            low=np.array([0, 0]),
            high=np.array([self.window_width, self.window_height])
        )

    def random_speed(self):
        return np.random.uniform(low=self.min_speed, high=self.max_speed)

    def random_heading(self):
        return np.random.uniform(low=0, high=2 * math.pi)

    def build_observation_space(self):
        s = spaces.Dict({
            'pos_x': spaces.Box(low=0, high=self.window_width, shape=(1,), dtype=np.float32),
            'pos_y': spaces.Box(low=0, high=self.window_height, shape=(1,), dtype=np.float32),
            'vel_x': spaces.Box(low=-self.max_speed, high=self.max_speed, shape=(1,), dtype=np.float32),
            'vel_y': spaces.Box(low=-self.max_speed, high=self.max_speed, shape=(1,), dtype=np.float32),
            'speed': spaces.Box(low=self.min_speed, high=self.max_speed, shape=(1,), dtype=np.float32),
            'heading': spaces.Box(low=0, high=2 * math.pi, shape=(1,), dtype=np.float32),
            'goal_x': spaces.Box(low=0, high=self.window_width, shape=(1,), dtype=np.float32),
            'goal_y': spaces.Box(low=0, high=self.window_height, shape=(1,), dtype=np.float32),
        })

        return spaces.Tuple((s,) * self.num_aircraft)


class Aircraft:
    def __init__(self, id, position, speed, heading, goal_pos, goal_vertiport_id, sector_id=-1):
        self.id = id
        self.position = np.array(position, dtype=np.float32)
        self.speed = speed
        self.heading = heading  # rad
        vx = self.speed * math.cos(self.heading)
        vy = self.speed * math.sin(self.heading)
        self.velocity = np.array([vx, vy], dtype=np.float32)

        self.reward = 0
        self.goal = Goal(goal_pos)
        self.sub_goal = Goal(goal_pos)
        self.goal_vertiport_id = goal_vertiport_id
        dx, dy = self.goal.position - self.position
        self.heading = math.atan2(dy, dx)

        self.load_config()

        self.conflict_id_set = set()  # track id of the aircraft in conflict

        self.sector_id = sector_id

    def load_config(self):
        self.G = Config.G
        self.scale = Config.scale
        self.min_speed = Config.min_speed
        self.max_speed = Config.max_speed
        self.speed_sigma = Config.speed_sigma
        self.d_heading = Config.d_heading

    def step(self, a=1):
        self.speed = Config.init_speed + np.random.normal(0, self.speed_sigma)
        self.speed = max(self.min_speed, min(self.speed, self.max_speed))  # project to range
        self.heading += (a - 1) * self.d_heading + np.random.normal(0, Config.heading_sigma)
        vx = self.speed * math.cos(self.heading)
        vy = self.speed * math.sin(self.heading)
        self.velocity = np.array([vx, vy])

        self.position += self.velocity

    def __repr__(self):
        s = 'id: %d, pos: %.2f,%.2f, speed: %.2f, heading: %.2f goal: %.2f,%.2f, sub-goal: %.2f,%.2f' \
            % (self.id,
               self.position[0],
               self.position[1],
               self.speed,
               math.degrees(self.heading),
               self.goal.position[0],
               self.goal.position[1],
               self.sub_goal.position[0],
               self.sub_goal.position[1]
               )
        return s


class Goal:
    def __init__(self, position):
        self.position = position

    def __repr__(self):
        s = 'pos: %s' % self.position
        return s


class AircraftDict:
    # all of the aircraft object is stored in this class
    def __init__(self):
        self.ac_dict = OrderedDict()

    # how many aircraft currently en route
    @property
    def num_aircraft(self):
        return len(self.ac_dict)

    # add aircraft to dict
    def add(self, aircraft):
        # id should always be different
        assert aircraft.id not in self.ac_dict.keys(), 'aircraft id %d already in dict' % aircraft.id
        self.ac_dict[aircraft.id] = aircraft

    # remove aircraft from dict
    def remove(self, aircraft):
        try:
            del self.ac_dict[aircraft.id]
        except KeyError:
            pass

    # get aircraft by its id
    def get_aircraft_by_id(self, aircraft_id):
        return self.ac_dict[aircraft_id]


class VertiPort:
    def __init__(self, id, position):
        self.id = id
        self.position = np.array(position)
        self.clock_counter = 0
        self.time_next_aircraft = np.random.uniform(0, 60)

    # when the next aircraft will take off
    def generate_interval(self):
        self.time_next_aircraft = np.random.uniform(Config.time_interval_lower, Config.time_interval_upper)
        self.clock_counter = 0

    # add the clock counter by 1
    def step(self):
        self.clock_counter += 1

    def __repr__(self):
        return 'vertiport id: %d, pos: %s' % (self.id, self.position)


class Sector:
    def __init__(self, id, vertices):
        self.id = id  # id range: 0,1,2,3,4,5,6
        self.vertices = vertices

        self.controlled_aircraft_id = set()
        self.exited_aircraft_id = {}
        self.set_gate()

    def set_gate(self):
        # set the position of the gates of each sector
        from shapely.geometry import LineString
        if self.id == 0:
            self.exits = []

            for i in range(6):
                start = self.vertices[i - 1]
                end = self.vertices[i]
                line = LineString([start, end])
                angle = math.atan2((end - start)[1], (end - start)[0])
                points = [line.interpolate((i / 3), normalized=True) for i in range(1, 3)]

                self.exits.append(np.array([[points[0].x, points[0].y],
                                            [points[0].x - Config.sector_exit_len * math.cos(angle),
                                             points[0].y - Config.sector_exit_len * math.sin(angle)],
                                            [points[0].x + Config.sector_exit_len * math.cos(angle),
                                             points[0].y + Config.sector_exit_len * math.sin(angle)]]))

        else:
            self.exits = []
            for i in range(self.id + 1, self.id + 4):
                v1 = i
                v2 = i + 1
                start = self.vertices[v1 % 6]
                end = self.vertices[v2 % 6]
                line = LineString([start, end])
                angle = math.atan2((end - start)[1], (end - start)[0])
                points = [line.interpolate((i / 3), normalized=True) for i in range(1, 3)]

                self.exits.append(np.array([[points[0].x, points[0].y],
                                            [points[0].x - Config.sector_exit_len * math.cos(angle),
                                             points[0].y - Config.sector_exit_len * math.sin(angle)],
                                            [points[0].x + Config.sector_exit_len * math.cos(angle),
                                             points[0].y + Config.sector_exit_len * math.sin(angle)]]))

        self.exits = np.array(self.exits)

    def assign_exit(self, aircraft):
        # when aircraft enters this aircraft, assign an exit gate to it
        if self.in_sector(aircraft.goal.position):
            aircraft.sub_goal = Goal(aircraft.goal.position)
            aircraft.goal_exit_id = -1

        else:
            min_dist_to_goal = 9999
            for i, exit in enumerate(self.exits):
                exit_to_goal = dist(exit[0][0], exit[0][1], aircraft.goal.position[0], aircraft.goal.position[1])
                dist_to_exit = dist(exit[0][0], exit[0][1], aircraft.position[0], aircraft.position[1])
                total_dist = dist_to_exit + exit_to_goal
                if total_dist < min_dist_to_goal:
                    closest_exit = exit[0].copy()
                    min_dist_to_goal = total_dist
                    aircraft.goal_exit_id = i

            aircraft.sub_goal = Goal(closest_exit)

    def in_sector(self, point):
        return mpltPath.Path(self.vertices).contains_point(point)
        # return Polygon(self.vertices).contains(Point(point[0], point[1]))
        # return self.in_hull(point, self.vertices)

    # def in_hull(self, p, hull):
    #     """
    #     Test if points in `p` are in `hull`
    #
    #     `p` should be a `NxK` coordinates of `N` points in `K` dimensions
    #     `hull` is either a scipy.spatial.Delaunay object or the `MxK` array of the
    #     coordinates of `M` points in `K`dimensions for which Delaunay triangulation
    #     will be computed
    #     """
    #     from scipy.spatial import Delaunay
    #     if not isinstance(hull, Delaunay):
    #         hull = Delaunay(hull)
    #
    #     return hull.find_simplex(p) >= 0

    def __repr__(self):
        s = 'id: %d' \
            % (self.id)
        return s

def dist(x1, y1, x2, y2):
    dx = x1 - x2
    dy = y1 - y2
    return (dx ** 2 + dy ** 2) ** 0.5


def dot(v, w):
    x, y = v
    X, Y = w
    return x * X + y * Y


def length(v):
    x, y = v
    return math.sqrt(x * x + y * y)


def vector(b, e):
    x, y = b
    X, Y = e
    return (X - x, Y - y)


def unit(v):
    x, y = v
    mag = length(v)
    return (x / mag, y / mag)


def distance(p0, p1):
    return length(vector(p0, p1))


def scale(v, sc):
    x, y = v
    return (x * sc, y * sc)


def add(v, w):
    x, y = v
    X, Y = w
    return (x + X, y + Y)


def pnt2line(pnt, start, end):
    line_vec = vector(start, end)
    pnt_vec = vector(start, pnt)
    line_len = length(line_vec)
    line_unitvec = unit(line_vec)
    pnt_vec_scaled = scale(pnt_vec, 1.0 / line_len)
    t = dot(line_unitvec, pnt_vec_scaled)
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    nearest = scale(line_vec, t)
    dist = distance(nearest, pnt_vec)
    nearest = add(nearest, start)
    return (dist, nearest)
