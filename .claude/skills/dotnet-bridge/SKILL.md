---
name: dotnet-bridge
description: Explain Python, FastAPI, asyncio, Pydantic, pytest, uv, SQLAlchemy, httpx, Redis, Keycloak or any other API, library, error or design concept in C#/.NET terms, simply and briefly. Use whenever explaining code, a plan, a design, a library or an error to Saimoon (an experienced C#/.NET developer learning Python), including during /explain, plan reviews and code walkthroughs.
---

# .NET bridge: explain in C#/.NET terms, briefly

Saimoon is an experienced C#/.NET developer learning Python through this project. The goal is a 1-to-1
knowledge transfer: tie every new idea to something Saimoon already knows, then show where it differs.

## How to explain a concept
1. **Lead with the .NET equivalent**, in one line. Example: "An `async def` with `yield` is Python's
   `IAsyncEnumerable<T>`."
2. **Then the Python version**, in 1–3 short sentences.
3. **Side-by-side snippet only if it helps.** C# first, Python second, the same example, 8 lines max each.
4. **One "Gotcha" line** where the analogy breaks. Never let a near-match pass as exact; a wrong mental
   model causes bugs later.
5. **Basics get a one-line refresher, not a lecture.** For general ideas (DI, async/await, ORMs,
   middleware, JWTs, event loops), give one plain sentence on what the idea is, then focus on how Python
   does it differently.
6. **If no .NET analogy fits, say so** and explain it plainly. Don't force one.

## Keep it short
- Answer first. No preamble, no restating the question.
- Plain words, short sentences, bullets over paragraphs.
- One example, not three.
- A concept fits in about 5 bullets or 150 words. Go deeper only when asked.
- Exception: core logic (rate limiting, metering, billing). Still keep it short, but always add the
  tradeoff in a line or two. Saimoon must be able to defend it in an interview.
- Define jargon once, in brackets, the first time it appears.

## Example
> **.NET:** `HttpClient`: create it once and reuse it, or you run out of sockets.
> **Python:** `httpx.AsyncClient`, same rule. Create it at startup (FastAPI `lifespan`), close it at shutdown.
> **Gotcha:** there's no `IHttpClientFactory`; you manage the single instance yourself.

## Mapping cheat sheet
Use these as starting points. Read the gotcha before relying on a mapping.

### Project and file structure
| Python | .NET | Gotcha |
| --- | --- | --- |
| root `pyproject.toml` with `[tool.uv.workspace]` | `.sln` | |
| member folder (`gateway/`) with its own `pyproject.toml` | a project (`.csproj`) | |
| package folder (`src/tollgate_gateway/`) | the project's root namespace | Folder name = import name, in `snake_case`. |
| module (`limits.py`) | a `.cs` file *and* a namespace | Each file is its own namespace: `from tollgate_gateway.limits import TokenBucket`. Many classes and functions per file is normal. |
| `__init__.py` | (none) | Marks a folder as a package and runs on first import. Keep it nearly empty. |
| `src/` layout | the `src/` folder convention | In Python it does real work: tests must import the installed package, not stray local files, so packaging mistakes show up early. |
| `tests/` next to `src/` | a separate `*.Tests` project | Not a separate project; pytest just finds `test_*.py` files. |
| `conftest.py` | shared test fixtures / test base class | pytest loads it automatically for its folder and below. |
| `__main__.py`, `if __name__ == "__main__":` | `static void Main` | Runs only when the file is executed directly, not when imported. |
| `import x`, `from x import y` | `using` | An import *runs* the module's top-level code the first time. Avoid side effects there. Circular imports fail at runtime. |
| `.python-version` | `global.json` | Pins the Python version uv uses. |

### Program.cs and Startup.cs (FastAPI)
| ASP.NET Core | FastAPI | Gotcha |
| --- | --- | --- |
| `Program.cs` (`CreateBuilder` → `app.Run()`) | `main.py` with `app = FastAPI(lifespan=lifespan)` | There's no `Run()` in the file. Uvicorn imports `app` and runs it: `uvicorn tollgate_gateway.main:app`. |
| `ConfigureServices` / `builder.Services.Add...` | no container: create singletons in `lifespan`, store them on `app.state`, hand them out with `Depends` functions | |
| `Configure` / the middleware pipeline | `app.add_middleware(...)` | The last one added is the outermost, so it runs first. |
| Controller / `MapGroup` | `APIRouter` + `app.include_router(router)` | |
| `appsettings.json` + `IOptions<T>` | env vars (plus `.env` locally) read into one typed settings object | |
| `IHostedService` start/stop | code before `yield` in `lifespan` = startup; after `yield` = shutdown | |
| Worker Service (`Host...Build().Run()` + `BackgroundService`) | `asyncio.run(main())` in `__main__.py` | You write the loop and the graceful shutdown yourself. |
| `dotnet run` / `dotnet watch` | `uv run uvicorn tollgate_gateway.main:app --reload` | |

### Classes
| C# | Python | Gotcha |
| --- | --- | --- |
| `class Foo { }` | `class Foo:` | |
| constructor | `def __init__(self, ...):` | Fields are created by assigning `self.x = ...` in `__init__`. No field declarations. |
| `this` | `self` | An explicit first parameter on every instance method. |
| `public` / `private` | no modifiers; `_name` means "private by convention" | Nothing is enforced. |
| property `{ get; }` | `@property` | |
| `static` method | `@staticmethod` / `@classmethod` | `@classmethod` receives the class as `cls`; use it for factory methods. |
| `: Base`, `: base(...)` | `class B(A):`, `super().__init__(...)` | Multiple inheritance exists; avoid it. |
| `interface` | `Protocol` (structural) or `ABC` | |
| method overloading | not supported | Use default and keyword arguments instead. |
| `ToString()` | `__str__` / `__repr__` | |
| `Equals` / `GetHashCode` | `__eq__` / `__hash__` | `@dataclass` generates `__eq__` for you. |
| `record` | `@dataclass(frozen=True)` or a Pydantic model | Use Pydantic at boundaries (HTTP, config, events) and dataclasses inside. |
| `enum` | `enum.Enum` / `StrEnum` | |
| generics `Box<T>` | `class Box[T]:` (3.12 syntax) | Only type checkers see it; no runtime effect. |
| `null` | `None` | Compare with `is None`, not `== None`. |
| `const` / `readonly` | `UPPER_CASE` name, `typing.Final` | Convention plus tooling; not enforced at runtime. |
| `throw`, `try/catch/finally` | `raise`, `try/except/finally` | Custom exceptions subclass `Exception`. |
| `List<T>`, `Dictionary<K,V>`, `HashSet<T>`, `ValueTuple` | `list`, `dict`, `set`, `tuple` | |
| extension methods | (none) | Write a plain function. |

### Language and async
| Python | C#/.NET | Gotcha |
| --- | --- | --- |
| `async def` / `await` | `async Task` / `await` | Coroutines are cold: calling `f()` without `await` does nothing. C# Tasks start immediately. |
| asyncio event loop | the thread pool | One thread runs every request. A blocking call (`time.sleep`, sync I/O, heavy CPU) freezes *all* requests, not just one. |
| async generator (`async def` + `yield`), `async for` | `IAsyncEnumerable<T>`, `await foreach` | |
| `asyncio.gather` | `Task.WhenAll` | |
| `asyncio.create_task` | starting a Task without awaiting it | Runs on the same loop, not another thread. Keep a reference, or it can be garbage-collected. |
| `asyncio.wait_for(x, timeout)` | `.WaitAsync(timeout)` | Cancels the inner task on timeout. |
| `asyncio.Semaphore`, `asyncio.Lock` | `SemaphoreSlim` | |
| `asyncio.Queue` | `Channel<T>` | |
| `task.cancel()` raises `CancelledError` | `CancellationToken` | No token to pass around: cancellation arrives as an exception at the next `await`. Don't swallow it. |
| `with`, `async with` | `using`, `await using` | |
| decorator `@x` | attribute `[X]` | A decorator is code that wraps the function and runs at definition time. An attribute is passive metadata. |
| type hints, `X \| None` | types, `X?` | Hints aren't enforced at runtime. Only tools (and Pydantic) check them. |
| `@dataclass` | `record` | No validation. |
| `Protocol`, `ABC` | `interface`, `abstract class` | `Protocol` is structural: a class matches without declaring it. |
| list/dict comprehension | LINQ `Select`/`Where` | Runs eagerly. LINQ is deferred. |
| package folder with `__init__.py` | namespace / assembly | |
| `logging` | `ILogger` | |
| the GIL | (none) | Only one thread runs Python code at a time, so threads don't speed up CPU work. |

### Web API (FastAPI)
| FastAPI | ASP.NET Core | Gotcha |
| --- | --- | --- |
| `app = FastAPI()`, `@app.get(...)` | Minimal APIs, `app.MapGet(...)` | |
| Uvicorn | Kestrel | |
| Pydantic `BaseModel` | record + model binding + DataAnnotations/FluentValidation | Validates at runtime when created. It's the only place types are enforced. |
| `Depends(...)` | constructor/parameter injection | No container and no registered lifetimes. Dependencies are plain functions resolved per request. A dependency that uses `yield` acts like a scoped service disposed at the end of the request. |
| `lifespan` | `Program.cs` startup/shutdown, `IHostedService.StartAsync/StopAsync` | |
| middleware | middleware | Same idea. |
| `StreamingResponse` + async generator | returning `IAsyncEnumerable<T>`, or writing to `Response.Body` + `FlushAsync` | A client disconnect usually shows up as cancellation inside your generator. |
| automatic `/docs` (OpenAPI) | Swashbuckle / `AddOpenApi` | |
| `httpx.AsyncClient` | `HttpClient` | Reuse one instance; there's no factory. |

### Tooling, tests and data
| Python | .NET | Gotcha |
| --- | --- | --- |
| `uv` | `dotnet` CLI + NuGet | |
| `uv sync` | `dotnet restore` | |
| `uv run pytest` | `dotnet test` | |
| `pyproject.toml` | `.csproj` | |
| uv workspace (root `pyproject.toml`) | `.sln` + `Directory.Build.props` | |
| `uv.lock` | `packages.lock.json` | |
| `.venv` | the project's restored packages | Python installs packages into an environment, not a project. The venv is what isolates them. |
| Ruff | Roslyn analyzers + `dotnet format` + `.editorconfig` | |
| test function + plain `assert` | xUnit `[Fact]` + `Assert.*` | |
| pytest fixture | xUnit constructor / `IClassFixture<T>` | Injected by parameter name. A `yield` fixture is setup + teardown, like `IDisposable`. |
| `@pytest.mark.parametrize` | `[Theory]` + `[InlineData]` | |
| pytest-asyncio | xUnit `async Task` tests | Async tests need the plugin; they aren't built in. |
| SQLAlchemy 2.0 `AsyncSession` | EF Core `DbContext` | |
| Alembic | EF Core migrations | Review autogenerated migrations; they can miss changes. |
| settings from env vars | `IOptions<T>` | |

### Infrastructure
| Tollgate piece | Closest .NET / Azure thing | Gotcha |
| --- | --- | --- |
| billing consumer (long-running loop) | `BackgroundService` / Worker Service | |
| Redis Streams + consumer groups | Azure Service Bus / Event Hubs consumer | Each message must be acked (`XACK`). Unacked ones stay pending until reclaimed (`XAUTOCLAIM`). |
| Keycloak + JWKS validation | Duende IdentityServer / Entra ID + `AddJwtBearer` | |
| Prometheus + Grafana | Application Insights / Azure Monitor | |
| OpenTelemetry span | `System.Diagnostics.Activity` | |
| k3s + Helm | AKS + Helm | |

## Python best practices (vs C# habits)
- **Naming:** `snake_case` for files, functions and variables; `PascalCase` for classes; `UPPER_CASE` for
  constants. Ruff checks this.
- **Free functions are fine.** Not everything needs a class. A module of functions is often the cleanest design.
- **No one-class-per-file rule.** Group by feature: `limits.py`, `router.py`, `usage.py`.
- **Type-hint everything public.** Hints document intent; Pydantic enforces them at the boundaries.
- **Mutable default arguments are a trap:** `def f(items=[])` shares one list across every call. Default to
  `None` and create the list inside.
- **f-strings** (`f"{tenant_id}: {tokens}"`) are C#'s `$"..."` interpolation.
- **Docstrings** (`"""..."""` as a function's first line) replace XML doc comments.
- **Ask forgiveness, not permission:** try the operation and catch the exception, instead of checking
  first. It's the normal Python style, unlike the C# `TryParse` habit.
- **No blocking calls in `async def`.** Use async libraries (httpx, asyncpg, `redis.asyncio`). Ruff's
  `ASYNC` rules catch common mistakes.
- **No side effects at import time:** no network or database connections at module level. Do that in `lifespan`.
- **Absolute imports** (`from tollgate_gateway.limits import ...`), not relative ones.
