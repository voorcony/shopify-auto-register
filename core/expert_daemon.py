#!/usr/bin/env python3
"""
Expert A daemon — watches for tasks from B, responds automatically via DeepSeek V4-Pro.
B → pending_expert_tasks.json → this daemon → expert_responses.json → B
"""
import json, os, time, urllib.request

API_KEY = "sk-ef5c5e973d9445068453dd601597034e"
API_URL = "http://127.0.0.1:18080/v1/chat/completions"
TASK_FILE = os.path.expanduser("~/.hermes/logs/pending_expert_tasks.json")
RESPONSE_FILE = os.path.expanduser("~/.hermes/logs/expert_responses.json")

SYSTEM_PROMPT = """# Role: 幕后专家 A (DeepSeek V4-Pro)

铁律:
1. 只回答 invoke_expert_a 提交的任务
2. 不闲聊，不解释过程，只给结果
3. 输出格式: JSON (verdict/summary/issues/positive_feedback)
4. 涉及代码评审按 CRITICAL/MAJOR/MINOR 分级
5. 涉及接口定义按 contracts + data_models 格式
6. 涉及架构按 issues + suggestion 格式"""

def call_expert(task):
    body = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task}
        ],
        "max_tokens": 4096,
        "thinking": {"type": "disabled"}
    }
    req = urllib.request.Request(API_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f'{{"verdict":"REJECT","summary":"Expert A error: {str(e)}","issues":[],"positive_feedback":[]}}'

def load_responses():
    if os.path.exists(RESPONSE_FILE):
        with open(RESPONSE_FILE) as f:
            return json.load(f)
    return {}

def save_responses(data):
    with open(RESPONSE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def main():
    print("Expert A daemon started. Watching for tasks...", flush=True)
    while True:
        try:
            if not os.path.exists(TASK_FILE):
                time.sleep(2)
                continue

            with open(TASK_FILE) as f:
                tasks = json.load(f)

            responses = load_responses()
            changed = False

            for task in tasks:
                tid = task.get("id", "")
                if task.get("status") != "pending":
                    continue
                if tid in responses:
                    continue

                print(f"Processing task {tid}: {task['task'][:80]}...", flush=True)
                result = call_expert(task["task"])
                responses[tid] = {
                    "task_id": tid,
                    "response": result,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                task["status"] = "completed"
                changed = True

            if changed:
                with open(TASK_FILE, "w") as f:
                    json.dump(tasks, f, ensure_ascii=False, indent=2)
                save_responses(responses)

        except Exception as e:
            print(f"Error: {e}", flush=True)

        time.sleep(2)

if __name__ == "__main__":
    main()
