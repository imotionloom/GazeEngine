"""唯一随机源，每次启动随机播种。"""
import random

_rng = random.Random()

def rand():
    """[0,1)"""
    return _rng.random()

def rand_range(a, b):
    return a + _rng.random() * (b - a)

def rand_int(a, b):
    return _rng.randint(a, b)

def choice(seq):
    return _rng.choice(seq)

def gauss(mu, sigma):
    return _rng.gauss(mu, sigma)

def seed(n):
    """重置全局随机种子（测试用）。"""
    _rng.seed(n)
