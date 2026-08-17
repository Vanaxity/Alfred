# Contributing to Alfred

This guide explains the development workflow for extending Alfred with new tools, features, and improvements.

---

## 🚀 Development Philosophy

Alfred v2 follows **Test-Driven Development (TDD)** with a focus on:

1. **Modular design** — Each component (PromptBuilder, ToolExecutor, etc.) is independent
2. **Clean code** — No side effects, clear function names, minimal comments
3. **Fast iteration** — Write test → implement → verify → commit
4. **Explicit over implicit** — Clear error messages, no silent failures

---

## 📋 TDD Workflow (Day 4 Example: ToolExecutor)

### Step 1: Write a Failing Test

Create `build-system/test_tool_executor.py`:

```python
"""
ToolExecutor unit tests — Day 4

Run directly:
    python build-system/test_tool_executor.py
    
Covers:
  1. Basic tool registration and dispatch
  2. Tool guardrails (allowed/deny patterns)
  3. Validation on success/failure
  4. Mutation verification
  5. Retry on failure
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.tool_executor import (
    ToolExecutor, ToolResult, Guardrails, create_tool_executor
)

# Test 1: Basic dispatch
def test_register_and_execute():
    executor = ToolExecutor()
    
    async def handle_time(params, context):
        return ToolResult(success=True, output="14:30", tool_name="time")
    
    executor.register("time", handle_time)
    
    # Execute should work
    import asyncio
    result = asyncio.run(executor.execute("time", {}, {}))
    assert result.success
    assert result.output == "14:30"
    print("[+] Test 1: Basic dispatch PASS")

# Test 2: Guardrails (deny pattern)
def test_guardrails_deny_pattern():
    executor = ToolExecutor()
    
    async def handle_shell(params, context):
        return ToolResult(success=True, output="OK", tool_name="shell")
    
    executor.register(
        "shell",
        handle_shell,
        guardrails=Guardrails(
            allowed=True,
            deny_patterns=[r"rm -rf"]
        )
    )
    
    # Should reject "rm -rf" command
    import asyncio
    result = asyncio.run(executor.execute("shell", {"command": "rm -rf /"}, {}))
    assert not result.success
    assert "denied" in result.error.lower()
    print("[+] Test 2: Guardrails deny pattern PASS")

# Test 3: Validation
def test_validation_on_result():
    executor = ToolExecutor()
    
    async def handle_always_fails(params, context):
        return ToolResult(success=False, error="Intentional error", tool_name="test")
    
    def validator(result):
        return result.success  # Only accept success
    
    executor.register("test", handle_always_fails, validator=validator)
    
    # Should fail validation
    import asyncio
    result = asyncio.run(executor.execute("test", {}, {}))
    assert not result.success
    print("[+] Test 3: Validation on result PASS")

# Run all tests
if __name__ == "__main__":
    test_register_and_execute()
    test_guardrails_deny_pattern()
    test_validation_on_result()
    print("\n✓ All tests passed (3/3)")
```

**Run the test** (it will FAIL because ToolExecutor isn't complete):

```bash
python build-system/test_tool_executor.py

# Output:
# ModuleNotFoundError: cannot import name 'create_tool_executor'
# (or AssertionError if partial implementation)
```

### Step 2: Implement the Feature

**Edit `brain/v2/tool_executor.py`**:

Implement the missing parts (already has skeleton, you fill it in):

```python
class ToolExecutor:
    """Modular tool dispatch with guardrails and validation."""
    
    def __init__(self) -> None:
        self._handlers: Dict[str, ToolHandler] = {}
        self._guardrails: Dict[str, Guardrails] = {}
        self._validators: Dict[str, Callable[[ToolResult], bool]] = {}
    
    async def execute(
        self,
        tool_name: str,
        params: Dict[str, Any],
        context: Dict[str, Any]
    ) -> ToolResult:
        """Execute a tool with guardrails and validation."""
        
        # Check guardrails
        if tool_name not in self._handlers:
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' not found",
                tool_name=tool_name
            )
        
        guardrails = self._guardrails.get(tool_name, Guardrails())
        if not guardrails.allowed:
            return ToolResult(success=False, error="Tool not allowed", tool_name=tool_name)
        
        # Check deny patterns
        for deny_pattern in guardrails.deny_patterns:
            if re.search(deny_pattern, str(params)):
                return ToolResult(
                    success=False,
                    error=f"Command denied by guardrail: {deny_pattern}",
                    tool_name=tool_name
                )
        
        # Execute handler
        try:
            handler = self._handlers[tool_name]
            result = await handler(params, context)
            
            # Validate result if validator exists
            if tool_name in self._validators:
                validator = self._validators[tool_name]
                if not validator(result):
                    return ToolResult(
                        success=False,
                        error="Validation failed",
                        tool_name=tool_name
                    )
            
            return result
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=tool_name)
```

### Step 3: Add to Exports

**Edit `brain/v2/__init__.py`**:

```python
from .tool_executor import (
    ToolExecutor,
    ToolResult,
    Guardrails,
    create_tool_executor,
)

__all__ = [
    # ... existing exports ...
    "ToolExecutor",
    "ToolResult",
    "Guardrails",
    "create_tool_executor",
]
```

### Step 4: Verify the Test Passes

```bash
python build-system/test_tool_executor.py

# Output:
# [+] Test 1: Basic dispatch PASS
# [+] Test 2: Guardrails deny pattern PASS
# [+] Test 3: Validation on result PASS
# ✓ All tests passed (3/3)
```

### Step 5: Add a `__main__` Demo

**At the bottom of `brain/v2/tool_executor.py`**:

```python
if __name__ == "__main__":
    import asyncio
    
    executor = ToolExecutor()
    
    # Define a demo tool
    async def handle_calculator(params, context):
        expr = params.get("expression", "")
        try:
            result = eval(expr)  # Not recommended for production!
            return ToolResult(success=True, output=str(result), tool_name="calculator")
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name="calculator")
    
    executor.register("calculator", handle_calculator)
    
    # Execute demo
    result = asyncio.run(executor.execute("calculator", {"expression": "2+2"}, {}))
    print(f"Calculator: 2+2 = {result.output}")
    assert result.success
    print("✓ Demo passed")
```

### Step 6: Run Full Test Suite

Ensure you didn't break anything:

```bash
python build-system/test_prompt_builder.py   # 6/6
python build-system/test_context_manager.py  # 10/10
python build-system/test_llm_router.py        # 7/7
python build-system/test_tool_executor.py     # X/X (new)
```

### Step 7: Commit

```bash
git add brain/v2/tool_executor.py build-system/test_tool_executor.py brain/v2/__init__.py
git commit -m "feat: implement ToolExecutor core dispatch with guardrails

- Add ToolExecutor.execute() with validation and error handling
- Add Guardrails for per-tool safety rules (allow/deny patterns)
- Add retry logic on mutation-tool failure
- Add mutation verification stub (wired in Day 6)
- Tests: 3/3 passing (guardrails, validation, dispatch)
- Exports added to brain/v2/__init__.py
"
```

---

## 🛠️ Adding a New Tool (Day 5 Example: Calculator)

### Step 1: Write Test

```python
def test_calculator_tool():
    executor = create_tool_executor()
    
    import asyncio
    
    # Test: 2 + 2 = 4
    result = asyncio.run(executor.execute("calculator", {"expression": "2+2"}, {}))
    assert result.success
    assert result.output == "4"
    
    # Test: invalid expression
    result = asyncio.run(executor.execute("calculator", {"expression": "1/0"}, {}))
    assert not result.success  # Should fail gracefully
```

### Step 2: Implement Handler

```python
async def handle_calculator(params, context):
    """Safe math calculator (parses expression, avoids eval in prod)."""
    expr = params.get("expression", "")
    
    # Whitelist allowed characters
    if not all(c in "0123456789+-*/.() " for c in expr):
        return ToolResult(
            success=False,
            error="Invalid characters in expression",
            tool_name="calculator"
        )
    
    try:
        # Use ast.literal_eval for safety (or use numexpr library)
        result = eval(expr)  # TODO: Replace with safe evaluator
        return ToolResult(success=True, output=str(result), tool_name="calculator")
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool_name="calculator")
```

### Step 3: Register in `create_tool_executor()`

```python
def create_tool_executor() -> ToolExecutor:
    """Create and populate the tool executor with all handlers."""
    executor = ToolExecutor()
    
    # Time
    executor.register("time", handle_time)
    
    # Calculator (NEW)
    executor.register("calculator", handle_calculator)
    
    # ... more tools ...
    
    return executor
```

### Step 4: Add to Tool Descriptions

**In `brain/v2/conversation.py`**:

```python
def _get_tool_descriptions(self):
    return {
        # ... existing tools ...
        "calculator": {
            "description": "Evaluate a math expression safely.",
            "params": {"expression": "Expression like '2+2' or '(10*5)/2'"},
        },
    }
```

### Step 5: Verify

```bash
python build-system/test_tool_executor.py
# Should show calculator test passing

python build-system/phase1_d1d2_test.py
# Should show more tests passing (if calculator is used in a test)
```

---

## 🧪 Testing Guidelines

### Unit Tests (Per Module)

- **Location**: `build-system/test_*.py`
- **Naming**: `test_<feature>()`
- **Pattern**: Arrange → Act → Assert
- **Example**:

```python
def test_prompt_builder_token_budget():
    # Arrange
    pb = PromptBuilder(token_budget=100)
    
    # Act
    result = pb.assemble(
        identity="You are Alfred.",
        profile="User: Sam",
        tools=[],
        rules=[],
        skills=[],
        memory=[]
    )
    
    # Assert
    assert result.token_count <= 100
```

### Integration Tests

- **Location**: `build-system/phase1_*.py`
- **Pattern**: Call real API endpoints, verify responses
- **Run after**: Server is running (`python -m brain_api.server`)

### Running Tests

```bash
# Single test
python build-system/test_prompt_builder.py

# All tests
for test in build-system/test_*.py; do python "$test"; done

# Integration (requires running server)
python build-system/phase1_d1d2_test.py
```

---

## 🎯 Code Standards

### Naming
- Functions/variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`

### Docstrings
Keep them minimal. Only add if the name doesn't say it all:

```python
# ❌ TOO VERBOSE
def add_user(self, message: str) -> None:
    """
    Add a user message to the conversation history.
    This method increments the message counter and tracks
    the token count of the message.
    """

# ✅ CONCISE
def add_user(self, message: str) -> None:
    """Add a user message to the conversation history."""
    self._messages.append(Message(role="user", content=message))
```

### Comments
Only when the WHY is non-obvious:

```python
# ❌ OBVIOUS
count = len(text) // 4  # Divide by 4

# ✅ NECESSARY
count = len(text) // 4  # Fallback: assume 4 chars per token (tiktoken unavailable)
```

### Error Handling
Be explicit:

```python
# ❌ SWALLOWS ERRORS
try:
    result = do_something()
except:
    pass

# ✅ EXPLICIT
try:
    result = do_something()
except FileNotFoundError:
    log.error("Config file missing")
    return ToolResult(success=False, error="Config not found")
except Exception as e:
    log.error(f"Unexpected error: {e}")
    raise
```

### Type Hints
Use them everywhere:

```python
# ✅ CLEAR
def add_tool_result(self, tool_name: str, result_dict: Dict[str, Any]) -> None:
    msg = Message(role="tool", content=str(result_dict), metadata={"tool": tool_name})
    self._messages.append(msg)

# ❌ UNCLEAR
def add_tool_result(self, tool_name, result_dict):
    msg = Message(...)
```

---

## 📝 Commit Message Format

```
<type>: <short summary (under 50 chars)>

<detailed explanation (wrapped at 72 chars)>
If needed, explain the WHY and HOW.

- Bullet points for key changes
- Link issues if applicable

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
```

**Types**:
- `feat:` — New feature (Day 4 = new ToolExecutor)
- `fix:` — Bug fix (handle edge case)
- `refactor:` — Code restructure (no behavior change)
- `test:` — Test additions/updates
- `docs:` — Documentation
- `perf:` — Performance improvement

**Examples**:
```
feat: implement ToolExecutor core dispatch with guardrails

- Add ToolExecutor.execute() with validation and error handling
- Add Guardrails for per-tool safety rules (allow/deny patterns)
- Add retry logic on mutation-tool failure
- Tests: 3/3 passing (guardrails, validation, dispatch)
```

---

## 🔄 Pull Request Workflow

1. **Create a branch**
   ```bash
   git checkout -b feature/day4-tool-executor
   ```

2. **Make commits** (small, focused)
   ```bash
   git commit -m "feat: implement ToolExecutor.execute()"
   git commit -m "feat: add Guardrails validation"
   git commit -m "test: add ToolExecutor unit tests"
   ```

3. **Push**
   ```bash
   git push origin feature/day4-tool-executor
   ```

4. **Create PR** on GitHub (title, description, link issue)

5. **Get review** → address feedback

6. **Merge** when approved & all tests green

---

## 🚨 Common Pitfalls

### ❌ Writing code without a test
Tests define behavior. Write test first.

### ❌ Silently failing
Always return `ToolResult(success=False, error="...")` on error.

### ❌ Mixing concerns
ToolExecutor should dispatch tools, not fetch weather. Keep it focused.

### ❌ Giant commits
"Day 4 complete" is too broad. One feature per commit.

### ❌ Async/await confusion
Tools are `async def handler(params, context) -> ToolResult`  
But v1 tool handlers might be sync. Wrap them.

```python
# Wrapping a sync handler as async
async def handle_time(params, context):
    # Old: time_tool = get_old_time_tool()
    #      result = time_tool.execute(params)
    # New:
    from brain.tools import old_time_tool
    result = old_time_tool.execute(params)  # Still sync, that's OK
    return ToolResult(success=True, output=result)
```

---

## 📞 Getting Help

- **Architecture questions**: Read [ARCHITECTURE.md](ARCHITECTURE.md)
- **How to test**: See this guide's testing section
- **Design discussion**: Ask in comments/PR

---

## 🎓 Learning Path

1. **Understand the Loop** — Read [ARCHITECTURE.md](ARCHITECTURE.md)
2. **Run existing tests** — `python build-system/test_prompt_builder.py`
3. **Pick a Day** — Start with Day 4 (ToolExecutor) or Day 5 (tool handlers)
4. **Follow TDD** — Write test → implement → commit
5. **Iterate** — Run full test suite, verify no regressions

---

## ✅ Checklist Before Submitting

- [ ] Test written first (TDD)
- [ ] Feature implemented
- [ ] All unit tests pass
- [ ] No regressions in other tests
- [ ] Exports added to `__init__.py`
- [ ] `__main__` demo works
- [ ] Commit message clear
- [ ] Code follows style guide (snake_case, type hints, etc.)

---

**Ready to contribute?** Start with [ARCHITECTURE.md](ARCHITECTURE.md), then pick a task from [docs/PHASES.md](docs/PHASES.md).

Good luck! 🚀
