# Blast Radius Analyzer

Static analysis tool to predict impact of code changes in multi-service systems.

## Features

- **Docker Compose Parser**: Extract service dependencies, ports, environment
- **Migration Parser**: Extract DB schema from Alembic migrations
- **Model Parser**: Extract SQLAlchemy model definitions
- **Sync Checker**: Detect mismatches between models and migrations

## Usage

```bash
# Analyze vovan-space project
python impact_analyzer.py --project /path/to/vovan-space

# With custom profile
python impact_analyzer.py --project /path/to/project --profile profiles/custom.json
```

## Example Output

```
============================================================
BLAST RADIUS ANALYZER - REPORT
============================================================

[SERVICES] Found 7 services:
  - launcher: ports=8000:8000, depends_on=[db]
  - zoo-galaxy-backend: ports=none, depends_on=[db]
  ...

[DATABASE] Found 4 tables in migrations:
  - users: 9 columns
  - tasks: 12 columns
  ...

[SYNC ISSUES] Found 4 issues:
  ⚠️  MISSING COLUMN: 'tasks.image_url' (String) exists in model but not in migrations
  ⚠️  MISSING COLUMN: 'tasks.title_en' (String) exists in model but not in migrations
  ...
```

## Roadmap

- [ ] Step 1: Static Spine (docker + SQL + models) ← **Current**
- [ ] Step 2: API Route Scanner (grep fetch calls)
- [ ] Step 3: Git diff integration
- [ ] Step 4: LLM Reasoner for impact explanation
