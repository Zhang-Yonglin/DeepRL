#######################################################################
# Copyright (C) 2017 Shangtong Zhang(zhangshangtong.cpp@gmail.com)    #
# Permission given to modify the code as long as you keep this        #
# declaration at the top                                              #
#######################################################################

from network import *
from component import *
from utils import *
import numpy as np
import time
import os
import pickle
import torch
from .BaseAgent import *

class QuantileRegressionDQNAgent(BaseAgent):
    def __init__(self, config):
        BaseAgent.__init__(self)
        self.config = config
        self.task = config.task_fn()
        self.network = config.network_fn(self.task.state_dim, self.task.action_dim)
        self.target_network = config.network_fn(self.task.state_dim, self.task.action_dim)
        self.optimizer = config.optimizer_fn(self.network.parameters())
        self.criterion = nn.MSELoss()
        self.target_network.load_state_dict(self.network.state_dict())
        self.replay = config.replay_fn()
        self.policy = config.policy_fn()
        self.total_steps = 0
        self.quantile_weight = 1.0 / self.config.num_quantiles
        self.cumulative_density = self.network.tensor(
            (2 * np.arange(self.config.num_quantiles) + 1) / (2.0 * self.config.num_quantiles))

    def huber(self, x):
        cond = (x < 1.0).float().detach()
        return 0.5 * x.pow(2) * cond + (x.abs() - 0.5) * (1 - cond)

    def episode(self, deterministic=False):
        episode_start_time = time.time()
        state = self.task.reset()
        total_reward = 0.0
        steps = 0
        while True:
            value = self.network.predict(np.stack([self.config.state_normalizer(state)])).squeeze(0).data
            value = (value * self.quantile_weight).sum(-1).cpu().numpy().flatten()
            if deterministic:
                action = np.argmax(value)
            elif self.total_steps < self.config.exploration_steps:
                action = np.random.randint(0, len(value))
            else:
                action = self.policy.sample(value)
            next_state, reward, done, _ = self.task.step(action)
            total_reward += reward
            reward = self.config.reward_normalizer(reward)
            if not deterministic:
                self.replay.feed([state, action, reward, next_state, int(done)])
                self.total_steps += 1
            steps += 1
            state = next_state
            if done:
                break
            if not deterministic and self.total_steps > self.config.exploration_steps:
                experiences = self.replay.sample()
                states, actions, rewards, next_states, terminals = experiences
                states = self.config.state_normalizer(states)
                next_states = self.config.state_normalizer(next_states)

                quantiles_next = self.target_network.predict(next_states).data
                q_next = (quantiles_next * self.quantile_weight).sum(-1)
                _, a_next = torch.max(q_next, dim=1)
                a_next = a_next.view(-1, 1, 1).expand(-1, -1, quantiles_next.size(2))
                quantiles_next = quantiles_next.gather(1, a_next).squeeze(1)

                rewards = self.network.tensor(rewards)
                terminals = self.network.tensor(terminals)
                quantiles_next = rewards.view(-1, 1) + self.config.discount * (1 - terminals.view(-1, 1)) * quantiles_next

                quantiles = self.network.predict(states)
                actions = self.network.tensor(actions, torch.LongTensor)
                actions = actions.view(-1, 1, 1).expand(-1, -1, quantiles.size(2))
                quantiles = quantiles.gather(1, Variable(actions)).squeeze(1)

                quantiles_next = quantiles_next.t().unsqueeze(-1)
                diff = Variable(quantiles_next) - quantiles
                loss = self.huber(diff) * Variable(self.cumulative_density.view(1, -1) - (diff.data < 0).float()).abs()

                self.optimizer.zero_grad()
                loss.mean(1).sum().backward()
                self.optimizer.step()
            if not deterministic and self.total_steps % self.config.target_network_update_freq == 0:
                self.target_network.load_state_dict(self.network.state_dict())
            if not deterministic and self.total_steps > self.config.exploration_steps:
                self.policy.update_epsilon()
        episode_time = time.time() - episode_start_time
        self.config.logger.debug('episode steps %d, episode time %f, time per step %f' %
                          (steps, episode_time, episode_time / float(steps)))
        return total_reward, steps
