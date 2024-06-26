import random
import time
import pickle
import torch
import numpy as np
from torch.utils.data import Dataset
from decision_transformer.d4rl_infos import REF_MIN_SCORE, REF_MAX_SCORE, D4RL_DATASET_STATS

np.set_printoptions(suppress=True)
np.set_printoptions(precision=6, linewidth=100)

def discount_cumsum(x, gamma):
    disc_cumsum = np.zeros_like(x)
    disc_cumsum[-1] = x[-1]
    for t in reversed(range(x.shape[0]-1)):
        disc_cumsum[t] = x[t] + gamma * disc_cumsum[t+1]
    return disc_cumsum


def get_d4rl_normalized_score(score, env_name):
    # assert env_key in REF_MAX_SCORE, f'no reference score for {env_key} env to calculate d4rl score'
    return (score - REF_MIN_SCORE[env_name]) / (REF_MAX_SCORE[env_name] - REF_MIN_SCORE[env_name])


def get_d4rl_dataset_stats(env_d4rl_name):
    return D4RL_DATASET_STATS[env_d4rl_name]


def evaluate_on_env(
        model, device, context_len, env, rtg_target, rtg_scale,
        num_eval_ep=10, max_test_ep_len=999,
        state_mean=None, state_std=None, discrete_action=False
):
    eval_batch_size = 1  # required for forward pass

    results = {}
    total_reward = 0
    total_timesteps = 0

    state_dim = env.observation_space.shape[0]

    if discrete_action is True:
        act_dim = 1
    else:
        act_dim = env.action_space.shape[0]

    if state_mean is None:
        state_mean = torch.zeros((state_dim,)).to(device)
    else:
        state_mean = torch.from_numpy(state_mean).to(device)

    if state_std is None:
        state_std = torch.ones((state_dim,)).to(device)
    else:
        state_std = torch.from_numpy(state_std).to(device)

    # same as timesteps used for training the transformer
    # also, crashes if device is passed to arange()
    timesteps = torch.arange(start=0, end=max_test_ep_len, step=1)
    timesteps = timesteps.repeat(eval_batch_size, 1).to(device)

    model.eval()

    with torch.no_grad():
        for _ in range(num_eval_ep):
            # zeros place holders
            actions = torch.zeros(
                (eval_batch_size, max_test_ep_len, act_dim), dtype=torch.float32, device=device
            )
            states = torch.zeros(
                (eval_batch_size, max_test_ep_len, state_dim), dtype=torch.float32, device=device
            )
            rewards_to_go = torch.zeros(
                (eval_batch_size, max_test_ep_len, 1), dtype=torch.float32, device=device
            )

            # init episode
            running_state, _ = env.reset()
            running_reward = 0
            running_rtg = rtg_target / rtg_scale

            for t in range(max_test_ep_len):
                total_timesteps += 1

                # add state in placeholder and normalize
                states[0, t] = torch.from_numpy(running_state).to(device)
                states[0, t] = (states[0, t] - state_mean) / state_std

                # calculate running rtg and add it in placeholder
                running_rtg = running_rtg - (running_reward / rtg_scale)
                rewards_to_go[0, t] = running_rtg

                if t < context_len:
                    act_preds, _, _ = model.forward(
                        timesteps[:, :context_len],
                        states[:, :context_len],
                        actions[:, :context_len],
                        rewards_to_go[:, :context_len]
                    )
                    act = act_preds[0, t].detach()
                else:
                    act_preds, _, _ = model.forward(
                        timesteps[:, t-context_len+1:t+1],
                        states[:, t-context_len+1:t+1],
                        actions[:, t-context_len+1:t+1],
                        rewards_to_go[:, t-context_len+1:t+1]
                    )
                    act = act_preds[0, -1].detach()

                running_state, running_reward, terminated, truncated, _ = env.step(act.cpu().numpy())

                # add action in placeholder
                actions[0, t] = act

                total_reward += running_reward

                if terminated or truncated:
                    break

    results['eval/avg_score'] = total_reward / num_eval_ep
    results['eval/avg_ep_len'] = total_timesteps / num_eval_ep

    return results


class D4RLTrajectoryDataset(Dataset):
    def __init__(self, dataset_path, context_len, rtg_scale, discrete_action=False):
        self.context_len = context_len
        self.discrete_action = discrete_action

        # load dataset, where each episode is stored
        with open(dataset_path, 'rb') as f:
            self.trajectories = pickle.load(f)

        # calculate min len of traj, state mean and variance
        # and returns_to_go for all traj
        min_len = 10**6
        states = []
        for traj in self.trajectories:
            traj_len = traj['observations'].shape[0]
            min_len = min(min_len, traj_len)
            states.append(traj['observations'])
            # calculate returns to go and rescale them
            traj['returns_to_go'] = discount_cumsum(traj['rewards'], 1.0) / rtg_scale
            # if (traj['rewards'] > 0.0).sum() > 0:
            #     print(traj['rewards'] > 0.0, "!!!!")
            #     print(traj['rewards'], "!!!!")

        # used for input normalization
        states = np.concatenate(states, axis=0)
        self.state_mean, self.state_std = np.mean(states, axis=0), np.std(states, axis=0) + 1e-6

        # normalize states
        for traj in self.trajectories:
            traj['observations'] = (traj['observations'] - self.state_mean) / self.state_std

    def get_state_stats(self):
        return self.state_mean, self.state_std

    def __len__(self):
        return len(self.trajectories)

    def __getitem__(self, idx):
        traj = self.trajectories[idx]
        traj_len = traj['observations'].shape[0]

        if traj_len >= self.context_len:
            # [NOTE]
            # 에피소드 길이가 self.context_len 보다 길면 임의의 위치에서 self.context_len 길이만큼 잘라서 리턴
            si = random.randint(0, traj_len - self.context_len)

            states = torch.from_numpy(traj['observations'][si : si + self.context_len])
            actions = torch.from_numpy(traj['actions'][si : si + self.context_len])
            returns_to_go = torch.from_numpy(traj['returns_to_go'][si : si + self.context_len])
            timesteps = torch.arange(start=si, end=si+self.context_len, step=1)

            # all ones since no padding
            traj_mask = torch.ones(self.context_len, dtype=torch.long)

        else:
            # [NOTE]
            # 에피소드 길이가 self.context_len 보다 짧으면 전체 에피소드 뒤에 zero-padding을 하여
            # self.context_len 길이로 일관되게 맞춤
            padding_len = self.context_len - traj_len

            # padding with zeros
            states = torch.from_numpy(traj['observations'])
            states = torch.cat(
            [
                    states,
                    torch.zeros(([padding_len] + list(states.shape[1:])), dtype=states.dtype)
                ],
                dim=0
            )

            actions = torch.from_numpy(traj['actions'])
            actions = torch.cat(
            [
                    actions,
                    torch.zeros(([padding_len] + list(actions.shape[1:])), dtype=actions.dtype)
                ],
                dim=0
            )

            returns_to_go = torch.from_numpy(traj['returns_to_go'])
            returns_to_go = torch.cat(
            [
                    returns_to_go,
                    torch.zeros(([padding_len] + list(returns_to_go.shape[1:])), dtype=returns_to_go.dtype)
                ],
                dim=0
            )

            timesteps = torch.arange(start=0, end=self.context_len, step=1)

            traj_mask = torch.cat(
                [torch.ones(traj_len, dtype=torch.long),
                torch.zeros(padding_len, dtype=torch.long)],
                dim=0
            )

        return (
            timesteps.to(dtype=torch.int32),
            states.to(dtype=torch.float32),
            actions.to(dtype=torch.int32) if self.discrete_action is True else actions.to(dtype=torch.float32),
            returns_to_go.to(dtype=torch.float32),
            traj_mask.to(dtype=torch.float32)
        )
