# Blast Radius Analyzer

Static analysis tool to predict impact of code changes in multi-service systems.

## Features

- **Docker Compose Parser**: Extract service dependencies, ports, environment
- **Migration Parser**: Extract DB schema from Alembic migrations
- **Model Parser**: Extract SQLAlchemy model definitions
- **Sync Checker**: Detect mismatches between models and migrations
- **Idempotency Checker**: Detect non-idempotent migration patterns
- **FK Consistency Checker**: Validate foreign key references and migration order
- **Env Var Checker**: Detect undefined/unused environment variables
- **Circular Dependency Detector**: Find cycles in service depends_on graph
- **Git Diff Analyzer**: Map changed files to affected services and tables
- **Live DB Checker**: Compare migrations with actual PostgreSQL schema

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Basic analysis (static only)
python impact_analyzer.py --project /path/to/vovan-space

# With live database check
python impact_analyzer.py -p /path/to/project --db-url postgresql://user:pass@host:5432/db

# Using DATABASE_URL environment variable
export DATABASE_URL=postgresql://user:pass@host:5432/db
python impact_analyzer.py -p /path/to/project

# JSON output
python impact_analyzer.py -p /path/to/project --json

# Analyze git diff impact
python impact_analyzer.py -p /path/to/project --diff HEAD~1
python impact_analyzer.py -p /path/to/project --diff main

# With custom profile
python impact_analyzer.py -p /path/to/project --profile profiles/custom.json
```

## Example Output

```
============================================================
BLAST RADIUS ANALYZER - REPORT
============================================================

[SERVICES] Found 9 services:
  - launcher: ports=none, depends_on=[db]
  - zoo-galaxy-backend: ports=none, depends_on=[db]
  ...

[DATABASE] Found 7 tables in migrations:
  - users: 10 columns
  - tasks: 16 columns
  - achievements: 14 columns
  ...

[MODELS] Found 7 models:
  - users: 10 fields
  - tasks: 16 fields
  ...

[SYNC] All models match migrations

[IDEMPOTENCY] All migrations are idempotent

[FOREIGN KEYS] Found 8 FK definitions
[FK] All foreign keys are consistent

[LIVE DB] Connected, found 7 tables

[SCHEMA DRIFT] Found 2 issues:

  MISSING COLUMNS (in migrations, not in DB):
    - Column 'achievements.name_en' defined in migrations but missing in database

  EXTRA COLUMNS (in DB, not in migrations):
    - Column 'users.legacy_field' exists in database but not in migrations

============================================================
```

## Checks Performed

| Check | What it detects | Severity |
|-------|----------------|----------|
| **Sync Check** | Model fields missing in migrations | Critical |
| **Idempotency** | Migrations that can fail on retry | Warning |
| **FK Missing Table** | FK references non-existent table | Critical |
| **FK Wrong Order** | FK defined before target table created | Critical |
| **Env Not Used** | Env var defined but not found in code | Warning |
| **Env Not Defined** | Env var used but not in docker-compose | Info |
| **Circular Deps** | Service A -> B -> C -> A dependency cycle | Critical |
| **High Risk Diff** | Git changes touch migrations + core tables | Critical |
| **Missing Table** | Table in migrations, not in DB | Critical |
| **Missing Column** | Column in migrations, not in DB | Critical |
| **Extra Table** | Table in DB, not in migrations | Warning |
| **Extra Column** | Column in DB, not in migrations | Warning |
| **Type Mismatch** | Column type differs between migration and DB | Warning |

## Exit Codes

- `0` - No critical issues found
- `1` - Critical issues detected (sync issues, missing tables/columns, non-idempotent migrations)

## Profile Configuration

```json
{
  "project_name": "vovan-space",
  "infrastructure": "docker-compose.yml",
  "db_schema": "public"
}
```

## Roadmap

- [x] Step 1: Static Spine (docker + SQL + models)
- [x] Step 2: Idempotency Checker
- [x] Step 3: Live Database Schema Comparison
- [x] Step 4: Foreign Key Consistency Check
- [x] Step 5: Environment Variable Validation
- [x] Step 6: Circular Dependency Detection
- [x] Step 7: Git diff integration
- [ ] Step 8: LLM Reasoner for impact explanation
