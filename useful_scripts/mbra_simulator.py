import random
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.metrics.pairwise import cosine_similarity
import time

# --- CONSTANTS & CONFIGURATION ---
EXPERIMENTS = 20   # Number of full simulation sessions
TRIALS = 1000      # Requests per session
KNOBS = {
    "W_PRIORITY": 1.0,
    "W_RELIABILITY": 3.0, 
    "W_ECOLOGY": 2.0,
    "W_SEMANTIC": 4.0
}

PROMPTS = [
    {"text": "Hello!", "c": 0.05, "tags": ["greeting"]},
    {"text": "Debug this Rust memory leak.", "c": 0.9, "tags": ["code", "analysis"]},
    {"text": "Summarize this medical case.", "c": 0.7, "tags": ["medical", "data"]},
    {"text": "Calculate the orbital mechanics of Mars.", "c": 0.95, "tags": ["math", "analysis"]},
    {"text": "What's the weather like?", "c": 0.1, "tags": ["greeting"]}
]

UNIQUE_TAGS = sorted(list(set([t for p in PROMPTS for t in p["tags"]])))
TAG_MAP = {tag: i for i, tag in enumerate(UNIQUE_TAGS)}
DIM = len(UNIQUE_TAGS)

class ServerNode:
    def __init__(self, name, priority, success_rate, tps, sigma, profile_vec, rate_limit=5):
        self.name = name
        self.priority = priority
        self.base_success_rate = success_rate
        self.avg_tps = tps
        self.sigma = sigma
        self.profile_vec = np.array([profile_vec])
        
        # State Tracking
        self.alpha = 10.0
        self.beta = 1.0
        self.active_requests = 0
        self.rate_limit = rate_limit
        self.is_oom = False
        self.oom_cooldown = 0

    def sample_reliability(self):
        return random.betavariate(self.alpha, self.beta)

    def process_request(self, complexity):
        # 1. Handle OOM Outages
        if self.is_oom:
            self.oom_cooldown -= 1
            if self.oom_cooldown <= 0: self.is_oom = False
            return False, 0, "NODE_OOM"

        # 2. Simulate Random OOM Crash (Giants crash more on long prompts)
        if self.sigma == 3 and complexity > 0.8 and random.random() < 0.05:
            self.is_oom = True
            self.oom_cooldown = 20 # Out for 20 trials
            return False, 0, "CRASH_OOM"

        # 3. Simulate Rate Limiting
        if self.active_requests >= self.rate_limit:
            return False, 0, "RATE_LIMIT_EXCEEDED"

        # 4. Success Logic
        self.active_requests += 1
        success = random.random() < self.base_success_rate
        
        # Jittered Latency
        latency = max(0.1, (1.0 / self.avg_tps) + np.random.normal(0, 0.05))
        
        self.active_requests -= 1
        if not success: return False, 0, "NETWORK_TIMEOUT"
        
        return True, self.avg_tps, "SUCCESS"

def run_simulation():
    algos = ["RoundRobin", "FixedPriority", "ReliabilityOnly", "SB-MRA (Ours)"]
    
    # Cumulative stats across all experiments
    # {algo: { metric: [value_per_experiment] }}
    stats_accumulator = {a: {"success": [], "waste": [], "dist": []} for a in algos}

    print(f"🚀 Starting Monte Carlo Simulation: {EXPERIMENTS} sessions of {TRIALS} trials...")

    for exp in range(EXPERIMENTS):
        # RESET CLUSTER FOR EACH EXPERIMENT
        def create_vec(active_tags):
            v = [0.0] * DIM
            for t in active_tags: v[TAG_MAP[t]] = 1.0
            return v

        nodes = [
            ServerNode("Giant-GPU", priority=1, success_rate=0.99, tps=80, sigma=3, profile_vec=[0.5]*DIM, rate_limit=10),
            ServerNode("Code-Specialist", priority=5, success_rate=0.95, tps=40, sigma=2, profile_vec=create_vec(['code', 'analysis'])),
            ServerNode("Math-Specialist", priority=5, success_rate=0.96, tps=35, sigma=2, profile_vec=create_vec(['math'])),
            ServerNode("Tiny-Llama", priority=10, success_rate=0.98, tps=120, sigma=1, profile_vec=create_vec(['greeting'])),
            ServerNode("Unstable-Cloud", priority=2, success_rate=0.70, tps=100, sigma=3, profile_vec=[0.5]*DIM, rate_limit=2)
        ]

        session_metrics = {a: {"success_count": 0, "total_waste": 0, "total_dist": 0} for a in algos}

        for i in range(TRIALS):
        prompt = random.choice(PROMPTS)
        p_complexity = prompt["c"]
        p_vec = np.array([create_vec(prompt["tags"])])
        # Add Semantic Noise
        p_vec += np.random.normal(0, 0.1, p_vec.shape)
        p_vec = np.clip(p_vec, 0, 1)

        for algo in algos:
            chosen_node = None
            
            if algo == "RoundRobin":
                chosen_node = nodes[i % len(nodes)]
            elif algo == "FixedPriority":
                # Blindly pick Giant-GPU
                chosen_node = nodes[0]
            elif algo == "ReliabilityOnly":
                chosen_node = max(nodes, key=lambda n: n.sample_reliability())
            elif algo == "SB-MRA (Ours)":
                scores = []
                for n in nodes:
                    rel_p = (1.0 - n.sample_reliability()) * 100
                    epp = (n.sigma - (p_complexity * 3))**2 if n.sigma > (p_complexity * 3) else 0
                    dist = 1.0 - cosine_similarity(p_vec, n.profile_vec)[0][0]
                    
                    s = (KNOBS["W_PRIORITY"] * n.priority) + \
                        (KNOBS["W_RELIABILITY"] * rel_p) + \
                        (KNOBS["W_ECOLOGY"] * epp * 10) + \
                        (KNOBS["W_SEMANTIC"] * dist * 100)
                    scores.append((s, n))
                chosen_node = min(scores, key=lambda x: x[0])[1]

            # EXECUTE
            success, tps, err_code = chosen_node.process_request(p_complexity)
            
            # UPDATE
            if algo == "SB-MRA (Ours)":
                # Fractional Bayesian Update
                if success:
                    reward = min(1.0, tps / 80.0)
                    chosen_node.alpha = (chosen_node.alpha * 0.95) + reward
                    chosen_node.beta = (chosen_node.beta * 0.95) + (1.0 - reward)
                else:
                    chosen_node.beta += 1.0 # Significant penalty for failure

            # LOGGING
            if success: metrics[algo]["success_count"] += 1
            else: metrics[algo]["failures"][err_code] = metrics[algo]["failures"].get(err_code, 0) + 1
            
            metrics[algo]["total_waste"] += max(0, chosen_node.sigma - (p_complexity * 3))
            metrics[algo]["total_dist"] += 1.0 - cosine_similarity(p_vec, chosen_node.profile_vec)[0][0]
            metrics[algo]["history"].append(metrics[algo]["success_count"] / (i + 1))

            # LOGGING SESSION
            session_metrics[algo]["success_count"] += 1 if success else 0
            session_metrics[algo]["total_waste"] += max(0, chosen_node.sigma - (p_complexity * 3))
            session_metrics[algo]["total_dist"] += 1.0 - cosine_similarity(p_vec, chosen_node.profile_vec)[0][0]

        # Store results of this session
        for a in algos:
            stats_accumulator[a]["success"].append(session_metrics[a]["success_count"] / TRIALS)
            stats_accumulator[a]["waste"].append(session_metrics[a]["total_waste"] / TRIALS)
            stats_accumulator[a]["dist"].append(session_metrics[a]["total_dist"] / TRIALS)

    # FINAL STATISTICAL REPORT
    print("\n--- MONTE CARLO STATISTICAL ANALYSIS ---")
    report_data = []
    for a in algos:
        report_data.append({
            "Algorithm": a,
            "Success (Mean)": f"{np.mean(stats_accumulator[a]['success'])*100:.2f}%",
            "Success (Var)": f"{np.var(stats_accumulator[a]['success']):.6f}",
            "Waste (Mean)": f"{np.mean(stats_accumulator[a]['waste']):.3f}",
            "Waste (Var)": f"{np.var(stats_accumulator[a]['waste']):.6f}",
            "Sem. Dist (Mean)": f"{np.mean(stats_accumulator[a]['dist']):.3f}"
        })
    
    print(pd.DataFrame(report_data).to_string(index=False))

if __name__ == "__main__":
    run_simulation()