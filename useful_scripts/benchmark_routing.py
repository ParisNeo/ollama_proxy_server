import random
import numpy as np
import matplotlib.pyplot as plt

# Simulate B-IRRA Constants
ALPHA_PRIOR = 10.0
BETA_PRIOR = 1.0
DECAY = 0.95
BASELINE_TPS = 50.0

class MockServer:
    def __init__(self, name, true_success_rate, true_avg_tps):
        self.name = name
        self.true_success_rate = true_success_rate
        self.true_avg_tps = true_avg_tps
        self.alpha = ALPHA_PRIOR
        self.beta = BETA_PRIOR

    def sample(self):
        # Thompson Sampling
        return random.betavariate(self.alpha, self.beta)

    def process_request(self):
        success = random.random() < self.true_success_rate
        if not success:
            return False, 0
        # TPS with some noise
        tps = max(1, self.true_avg_tps + np.random.normal(0, 5))
        return True, tps

    def update(self, success, tps=0):
        # Apply Decay
        self.alpha = max(1.0, self.alpha * DECAY)
        self.beta = max(1.0, self.beta * DECAY)
        
        if success:
            reward = min(1.0, tps / BASELINE_TPS)
            self.alpha += reward
            self.beta += (1.0 - reward)
        else:
            self.beta += 1.0

def run_benchmark(iterations=200):
    servers = [
        MockServer("High-End GPU", 0.99, 85.0),
        MockServer("Stable CPU", 0.98, 12.0),
        MockServer("Unstable Cloud", 0.60, 100.0)
    ]
    
    history = {s.name: [] for s in servers}
    selections = {s.name: 0 for s in servers}

    for i in range(iterations):
        # 1. Selection Phase
        samples = [(s.sample(), s) for s in servers]
        # B-IRRA score (Lower is better)
        # Assuming all priorities are equal (10)
        best_server = min(samples, key=lambda x: 10 + (1.0 - x[0]) * 100)[1]
        
        # 2. Execution Phase
        success, tps = best_server.process_request()
        best_server.update(success, tps)
        
        # 3. Log State
        selections[best_server.name] += 1
        for s in servers:
            history[s.name].append(s.alpha / (s.alpha + s.beta))

    # --- Plotting ---
    plt.figure(figsize=(10, 6))
    for name, vals in history.items():
        plt.plot(vals, label=f"{name} (P_success)")
    
    plt.title("B-IRRA Posterior Convergence")
    plt.xlabel("Requests")
    plt.ylabel("Expected Success Probability (Alpha / Total)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig("benchmark_results.png")
    
    print("Benchmark complete. Results saved to benchmark_results.png")
    print(f"Selection Stats: {selections}")

if __name__ == "__main__":
    run_benchmark()