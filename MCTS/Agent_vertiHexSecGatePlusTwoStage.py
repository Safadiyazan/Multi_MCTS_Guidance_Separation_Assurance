import argparse
import numpy as np
import time

import sys

sys.path.extend(['../Simulators'])
from nodesHexSecGatePlus import MultiAircraftNode, MultiAircraftState
from search_multi import MCTS
from config_hex_sec import Config
from MultiAircraftVertiHexSecGatePlusTwoStageEnv import MultiAircraftEnv

np.set_printoptions(linewidth=9999, precision=3, threshold=99999, suppress=True)


def run_experiment(env, no_episodes, render, save_path):
    text_file = open(save_path, "w")  # save all non-terminal print statements in a txt file
    episode = 0
    epi_returns = []
    conflicts_list = []
    num_aircraft = Config.num_aircraft
    time_dict = {}
    route_time = {1: [], 2: [], 3: []}

    while episode < no_episodes:
        # at the beginning of each episode, set done to False, set time step in this episode to 0
        # set reward to 0, reset the environment
        episode += 1
        done = False
        episode_time_step = 0
        episode_reward = 0
        last_observation = env.reset()
        action = np.ones(num_aircraft)
        info = None
        near_end = False
        counter = 0  # avoid end episode initially

        while not done:
            if render:
                env.render()
            if episode_time_step % 5 == 0:
                # if env.id_tracker > 1300 and env.debug:
                #     import ipdb; ipdb.set_trace()
                action_by_id = {}
                for i in range(7):

                    ob_high_in, id_high, goal_exit_id_high, ob_high_out, \
                    ob_in, id, goal_exit_id, ob_out = last_observation[i]

                    time_before = int(round(time.time() * 1000))

                    # make decision for high priority aircraft
                    # ----------------------------------------
                    ob_high = np.concatenate([ob_high_in, ob_high_out])
                    num_considered_aircraft = len(id_high)
                    num_existing_aircraft = ob_high.shape[0]
                    action_high = np.ones(num_existing_aircraft, dtype=np.int32)

                    for index in range(num_considered_aircraft):
                        state = MultiAircraftState(state=ob_high,
                                                   index=index,
                                                   init_action=action_high,
                                                   sector_id=i,
                                                   goal_exit_id=goal_exit_id_high[index])
                        root = MultiAircraftNode(state=state)
                        mcts = MCTS(root)
                        if info[id_high[index]] < 5 * Config.minimum_separation:
                            best_node = mcts.best_action(Config.no_simulations, Config.search_depth)
                        else:
                            best_node = mcts.best_action(Config.no_simulations_lite, Config.search_depth_lite)

                        # if id_list[index] == 103 or id_list[index] == 123:
                        #     if env.id_tracker > 130:
                        #         import ipdb; ipdb.set_trace()

                        action_high[index] = best_node.state.prev_action[index]
                        action_by_id[id_high[index]] = best_node.state.prev_action[index]

                    # make decision for low priority aircraft
                    # ---------------------------------------
                    ob = np.concatenate([ob_in, ob_high_in, ob_high_out, ob_out])
                    num_considered_aircraft = len(id)
                    if not ob_in.shape[0] == num_considered_aircraft:
                        raise ValueError('error dimension')
                    num_existing_aircraft = ob.shape[0]
                    action = np.ones(num_existing_aircraft, dtype=np.int32)
                    action[num_considered_aircraft:num_considered_aircraft + action_high.shape[0]] = action_high

                    for index in range(num_considered_aircraft):
                        state = MultiAircraftState(state=ob,
                                                   index=index,
                                                   init_action=action,
                                                   sector_id=i,
                                                   goal_exit_id=goal_exit_id[index])
                        root = MultiAircraftNode(state=state)
                        mcts = MCTS(root)
                        if info[id[index]] < 5 * Config.minimum_separation:
                            best_node = mcts.best_action(Config.no_simulations, Config.search_depth)
                        else:
                            best_node = mcts.best_action(Config.no_simulations_lite, Config.search_depth_lite)

                        action[index] = best_node.state.prev_action[index]
                        action_by_id[id[index]] = best_node.state.prev_action[index]

                    # decision making end

                    time_after = int(round(time.time() * 1000))
                    if num_considered_aircraft in time_dict:
                        time_dict[num_considered_aircraft].append(time_after - time_before)
                    else:
                        time_dict[num_considered_aircraft] = [time_after - time_before]

            observation, reward, done, info = env.step(action_by_id, near_end)

            episode_reward += reward
            last_observation = observation
            episode_time_step += 1

            if episode_time_step % 100 == 0:
                print('========================== Time Step: %d =============================' % episode_time_step,
                      file=text_file)
                print('Number of conflicts:', env.conflicts / 2, file=text_file)
                print('Total Aircraft Genrated:', env.id_tracker, file=text_file)
                print('Goal Aircraft:', env.goals, file=text_file)
                print('NMACs:', env.NMACs / 2, file=text_file)
                print('NMAC/h:', (env.NMACs / 2) / (env.total_timesteps / 3600), file=text_file)
                print('Current Aircraft Enroute:', env.aircraft_dict.num_aircraft, file=text_file)

                print('========================== Time Step: %d =============================' % episode_time_step)
                print('Number of conflicts:', env.conflicts / 2)
                print('Total Aircraft Genrated:', env.id_tracker)
                print('Goal Aircraft:', env.goals)
                print('NMACs:', env.NMACs / 2)
                print('NMAC/h:', (env.NMACs / 2) / (env.total_timesteps / 3600))
                print('Current Aircraft Enroute:', env.aircraft_dict.num_aircraft)

                print('Clear', np.array([305, 540, 610]))
                print('High Priority Route Time:',
                      np.array([np.mean(env.route_time[1][1]), np.mean(env.route_time[1][2]),
                                np.mean(env.route_time[1][3])]))
                print('Low  Priority Route Time:',
                      np.array([np.mean(env.route_time[0][1]), np.mean(env.route_time[0][2]),
                                np.mean(env.route_time[0][3])]))

            if env.id_tracker - 1 >= 10000:
                counter += 1
                near_end = True

            if episode_time_step > 10 and env.aircraft_dict.num_aircraft == 0:
                break

        # print('clear route time:', env.route_time)
        print('route 1 time:', env.route_time[0][1] + env.route_time[1][1], file=text_file)
        print('route 2 time:', env.route_time[0][2] + env.route_time[1][2], file=text_file)
        print('route 3 time:', env.route_time[0][3] + env.route_time[1][3], file=text_file)

        # print('========================== End =============================', file=text_file)
        # print('========================== End =============================')
        # print('Number of conflicts:', env.conflicts / 2)
        # print('Total Aircraft Genrated:', env.id_tracker)
        # print('Goal Aircraft:', env.goals)
        # print('NMACs:', env.NMACs / 2)
        # print('Current Aircraft Enroute:', env.aircraft_dict.num_aircraft)
        # for key, item in time_dict.items():
        #     print('%d aircraft: %.2f' % (key, np.mean(item)))
        #
        # # print training information for each training episode
        # epi_returns.append(info)
        # conflicts_list.append(env.conflicts)
        # print('Training Episode:', episode)
        # print('Cumulative Reward:', episode_reward)

    time_list = time_dict.values()
    flat_list = [item for sublist in time_list for item in sublist]
    print('----------------------------------------')
    print('Number of aircraft:', Config.num_aircraft)
    print('Search depth:', Config.search_depth)
    print('Simulations:', Config.no_simulations)
    print('Time:', sum(flat_list) / float(len(flat_list)))
    print('NMAC prob:', epi_returns.count('n') / no_episodes)
    print('Goal prob:', epi_returns.count('g') / no_episodes)
    print('Average Conflicts per episode:',
          sum(conflicts_list) / float(len(conflicts_list)) / 2)  # / 2 to ignore duplication
    env.close()
    text_file.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no_episodes', '-e', type=int, default=10)
    parser.add_argument('--seed', type=int, default=2)
    parser.add_argument('--save_path', '-p', type=str, default='output/seed2.txt')
    parser.add_argument('--debug', '-d', action='store_true')
    parser.add_argument('--render', '-r', action='store_true')
    args = parser.parse_args()

    import random
    random.seed(args.seed)
    np.random.seed(args.seed)

    env = MultiAircraftEnv(args.seed, args.debug)
    run_experiment(env, args.no_episodes, args.render, args.save_path)


if __name__ == '__main__':
    main()
