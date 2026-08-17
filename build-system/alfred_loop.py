# build-system/alfred_loop.py
import subprocess, time, json, sys
from pathlib import Path
from datetime import datetime

BACKLOG = Path.home() / '.local' / 'share' / 'opencode' / 'orchestrator' / 'backlog.json'
TEST = Path('build-system') / 'alfred_test_suite.py'
LOGS = Path('build-system') / 'logs'
LOGS.mkdir(exist_ok=True)

tasks = json.loads(BACKLOG.read_text())

for task in tasks:
    if task['status'] != 'ready':
        continue
    
    task['status'] = 'in-progress'
    BACKLOG.write_text(json.dumps(tasks, indent=2))
    
    log_file = LOGS / f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    print(f"[{datetime.now():%H:%M}] {task['title'][:80]}")
    
    prompt = (
        f"Read ALFRED_MANIFESTO_V5.md and build-system/PROJECT_TRACKER.md for context. "
        f"Then complete this task: {task['title']}. "
        f"After finishing, update PROJECT_TRACKER.md: mark this task [x] DONE."
    )
    
    with open(log_file, 'w', encoding='utf-8') as f:
        result = subprocess.run(
            ['opencode', 'run', prompt, '--dangerously-skip-permissions'],
            shell=True, stdout=f, stderr=subprocess.STDOUT,
            timeout=900, cwd=str(Path.cwd())
        )
    
    if result.returncode == 0:
        task['status'] = 'done'
        print("  Done")
    else:
        task['status'] = 'ready'
        print(f"  Failed (log: {log_file.name})")
    
    BACKLOG.write_text(json.dumps(tasks, indent=2))
    time.sleep(300)  # 5 min between tasks

# Run tests after all tasks done
print("\nRunning test suite...")
subprocess.run([sys.executable, str(TEST), '--phase', '1'])