import time, threading

class TokenBucket:
    def __init__(self, rate_per_min, capacity=None):
        self.rate_per_sec = rate_per_min / 60.0
        self.capacity = capacity or rate_per_min
        self.tokens = self.capacity
        self.last = time.perf_counter()
        self.lock = threading.Lock()
    def take(self, tokens=1):
        while True:
            with self.lock:
                now = time.perf_counter()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate_per_sec)
                self.last = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
            time.sleep(0.01)