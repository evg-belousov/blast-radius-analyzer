#!/usr/bin/env python3
"""
Blast Radius Analyzer - Impact Analysis for Code Changes

Detects sync issues between SQLAlchemy models and Alembic migrations,
and maps service dependencies from docker-compose.

Usage:
    python impact_analyzer.py --project /path/to/project
    python impact_analyzer.py -p /path/to/project --profile profiles/custom.json
"""

import os
import re
import yaml
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ServiceNode:
    """Docker Compose service."""
    name: str
    ports: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    build_context: Optional[str] = None


@dataclass
class TableColumn:
    """Database column from migration."""
    name: str
    type: str
    nullable: bool = True


@dataclass
class TableSchema:
    """Database table from migrations."""
    name: str
    columns: Dict[str, TableColumn] = field(default_factory=dict)


@dataclass
class ModelField:
    """SQLAlchemy model field."""
    name: str
    type: str


@dataclass
class IdempotencyIssue:
    """Non-idempotent migration issue."""
    file: str
    table: str
    columns: List[str]
    message: str


@dataclass
class AnalysisResult:
    """Complete analysis result."""
    services: Dict[str, ServiceNode] = field(default_factory=dict)
    db_tables: Dict[str, TableSchema] = field(default_factory=dict)
    model_fields: Dict[str, Dict[str, ModelField]] = field(default_factory=dict)
    sync_issues: List[str] = field(default_factory=list)
    idempotency_issues: List[IdempotencyIssue] = field(default_factory=list)
    dependency_graph: Dict[str, List[str]] = field(default_factory=dict)


# ============================================================================
# Parsers
# ============================================================================

class DockerComposeParser:
    """Parse docker-compose.yml for service dependencies."""

    def parse(self, filepath: str) -> Dict[str, ServiceNode]:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        services = {}
        for name, config in data.get('services', {}).items():
            services[name] = ServiceNode(
                name=name,
                ports=config.get('ports', []),
                depends_on=self._get_depends_on(config),
                environment=self._parse_env(config.get('environment', {})),
                build_context=self._get_build_context(config.get('build'))
            )
        return services

    def _get_depends_on(self, config: dict) -> List[str]:
        deps = config.get('depends_on', [])
        if isinstance(deps, dict):
            return list(deps.keys())
        return deps

    def _parse_env(self, env) -> Dict[str, str]:
        if isinstance(env, dict):
            return {k: str(v) for k, v in env.items()}
        if isinstance(env, list):
            result = {}
            for item in env:
                if '=' in str(item):
                    key, value = str(item).split('=', 1)
                    result[key] = value
            return result
        return {}

    def _get_build_context(self, build) -> Optional[str]:
        if isinstance(build, str):
            return build
        if isinstance(build, dict):
            return build.get('context')
        return None


class MigrationParser:
    """Parse Alembic migrations for DB schema."""

    def parse_directory(self, migrations_dir: str) -> Dict[str, TableSchema]:
        tables = {}
        migrations_path = Path(migrations_dir)

        if not migrations_path.exists():
            return tables

        for migration_file in sorted(migrations_path.glob('*.py')):
            self._parse_migration(migration_file, tables)

        return tables

    def _parse_migration(self, filepath: Path, tables: Dict[str, TableSchema]):
        content = filepath.read_text(encoding='utf-8')
        lines = content.split('\n')

        current_table = None
        paren_depth = 0
        in_create_table = False
        waiting_for_table_name = False

        for line in lines:
            # Detect create_table (may span multiple lines)
            if 'op.create_table(' in line:
                waiting_for_table_name = True
                in_create_table = True
                paren_depth = line.count('(') - line.count(')')

                # Check if table name is on same line
                after_create = line.split('create_table')[1] if 'create_table' in line else ""
                name_match = re.search(r"['\"](\w+)['\"]", after_create)
                if name_match:
                    current_table = name_match.group(1)
                    if current_table not in tables:
                        tables[current_table] = TableSchema(name=current_table)
                    waiting_for_table_name = False
                continue

            # Get table name from next line
            if waiting_for_table_name:
                name_match = re.search(r"['\"](\w+)['\"]", line)
                if name_match:
                    current_table = name_match.group(1)
                    if current_table not in tables:
                        tables[current_table] = TableSchema(name=current_table)
                    waiting_for_table_name = False
                paren_depth += line.count('(') - line.count(')')
                continue

            # Inside create_table block
            if in_create_table:
                paren_depth += line.count('(') - line.count(')')

                # Find Column: sa.Column('name', sa.Type(), ...)
                col_match = re.search(r"sa\.Column\s*\(\s*['\"](\w+)['\"].*?sa\.(\w+)\s*\(", line)
                if col_match and current_table:
                    tables[current_table].columns[col_match.group(1)] = TableColumn(
                        name=col_match.group(1),
                        type=col_match.group(2)
                    )

                # End of create_table
                if paren_depth <= 0:
                    in_create_table = False
                    current_table = None

            # Handle add_column for later migrations
            if 'op.add_column(' in line:
                add_match = re.search(
                    r"op\.add_column\s*\(\s*['\"](\w+)['\"].*?"
                    r"sa\.Column\s*\(\s*['\"](\w+)['\"].*?sa\.(\w+)\s*\(",
                    line
                )
                if add_match:
                    table_name, col_name, col_type = add_match.groups()
                    if table_name not in tables:
                        tables[table_name] = TableSchema(name=table_name)
                    tables[table_name].columns[col_name] = TableColumn(
                        name=col_name, type=col_type
                    )


class SQLAlchemyModelParser:
    """Parse SQLAlchemy models for field definitions."""

    def parse_file(self, filepath: str) -> Dict[str, Dict[str, ModelField]]:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        models = {}
        class_pattern = re.compile(r"class\s+(\w+)\s*\([^)]*Base[^)]*\):")
        tablename_pattern = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
        column_pattern = re.compile(r"(\w+)\s*=\s*Column\s*\(\s*(\w+)")

        # Split by class definitions
        class_blocks = re.split(r'(?=class\s+\w+\s*\([^)]*Base)', content)

        for block in class_blocks:
            class_match = class_pattern.search(block)
            if not class_match:
                continue

            # Get table name
            table_match = tablename_pattern.search(block)
            table_name = table_match.group(1) if table_match else class_match.group(1).lower()

            # Get fields
            fields = {}
            for col_match in column_pattern.finditer(block):
                field_name = col_match.group(1)
                if not field_name.startswith('_'):
                    fields[field_name] = ModelField(
                        name=field_name,
                        type=col_match.group(2)
                    )

            if fields:
                models[table_name] = fields

        return models


# ============================================================================
# Sync Checker
# ============================================================================

class SyncChecker:
    """Check for sync issues between models and migrations."""

    def check(
        self,
        db_tables: Dict[str, TableSchema],
        model_fields: Dict[str, Dict[str, ModelField]]
    ) -> List[str]:
        issues = []

        for table_name, fields in model_fields.items():
            if table_name not in db_tables:
                issues.append(
                    f"CRITICAL: Table '{table_name}' exists in models but not in migrations"
                )
                continue

            db_columns = db_tables[table_name].columns

            for field_name, field_info in fields.items():
                if field_name not in db_columns:
                    issues.append(
                        f"MISSING COLUMN: '{table_name}.{field_name}' ({field_info.type}) "
                        f"exists in model but not in migrations"
                    )

        return issues


# ============================================================================
# Idempotency Checker
# ============================================================================

class IdempotencyChecker:
    """Check for non-idempotent migration patterns.

    Detects migrations that use table_exists() check but don't handle
    the case where table exists but is missing columns (partial migration).
    """

    def check_directory(self, migrations_dir: str) -> List[IdempotencyIssue]:
        issues = []
        migrations_path = Path(migrations_dir)

        if not migrations_path.exists():
            return issues

        for migration_file in migrations_path.glob('*.py'):
            file_issues = self._check_migration(migration_file)
            issues.extend(file_issues)

        return issues

    def _check_migration(self, filepath: Path) -> List[IdempotencyIssue]:
        issues = []
        content = filepath.read_text(encoding='utf-8')

        # Find all table_exists checks with create_table
        # Pattern: if not table_exists('tablename'): ... create_table('tablename', ...)
        table_exists_pattern = re.compile(
            r"if\s+not\s+table_exists\s*\(\s*['\"](\w+)['\"]\s*\)",
            re.MULTILINE
        )

        for match in table_exists_pattern.finditer(content):
            table_name = match.group(1)
            check_pos = match.start()

            # Find the create_table block for this table
            columns = self._find_columns_in_create_table(content, table_name, check_pos)

            if not columns:
                continue

            # Check if there's an else block with column_exists checks
            columns_with_fallback = self._find_column_exists_checks(content, table_name, check_pos)

            # Find columns that are created but have no fallback
            unprotected_columns = [col for col in columns if col not in columns_with_fallback]

            if unprotected_columns:
                issues.append(IdempotencyIssue(
                    file=filepath.name,
                    table=table_name,
                    columns=unprotected_columns,
                    message=(
                        f"Non-idempotent migration: table '{table_name}' uses table_exists() "
                        f"but has no column_exists() fallback for: {', '.join(unprotected_columns)}. "
                        f"If migration fails mid-way, these columns won't be added on retry."
                    )
                ))

        return issues

    def _find_columns_in_create_table(self, content: str, table_name: str, after_pos: int) -> List[str]:
        """Find all columns defined in create_table for given table."""
        columns = []

        # Look for create_table('table_name', ...) after the table_exists check
        search_content = content[after_pos:]

        # Find create_table call
        create_pattern = re.compile(
            rf"op\.create_table\s*\(\s*['\"]({re.escape(table_name)})['\"]",
            re.MULTILINE
        )
        create_match = create_pattern.search(search_content)

        if not create_match:
            return columns

        # Find the block boundaries (count parentheses)
        start_pos = create_match.start()
        paren_depth = 0
        in_block = False
        block_content = []

        for i, char in enumerate(search_content[start_pos:]):
            if char == '(':
                paren_depth += 1
                in_block = True
            elif char == ')':
                paren_depth -= 1

            if in_block:
                block_content.append(char)

            if in_block and paren_depth == 0:
                break

        block_text = ''.join(block_content)

        # Find all Column definitions
        col_pattern = re.compile(r"sa\.Column\s*\(\s*['\"](\w+)['\"]")
        for col_match in col_pattern.finditer(block_text):
            col_name = col_match.group(1)
            # Skip common auto columns
            if col_name not in ('id',):
                columns.append(col_name)

        return columns

    def _find_column_exists_checks(self, content: str, table_name: str, after_pos: int) -> List[str]:
        """Find columns that have column_exists fallback checks."""
        protected_columns = []

        # Look for else block and column_exists checks
        search_content = content[after_pos:]

        # Pattern 1: column_exists('table', 'column')
        col_exists_pattern = re.compile(
            rf"column_exists\s*\(\s*['\"]({re.escape(table_name)})['\"],\s*['\"](\w+)['\"]",
            re.MULTILINE
        )

        for match in col_exists_pattern.finditer(search_content):
            if match.group(1) == table_name:
                protected_columns.append(match.group(2))

        # Pattern 2: ensure_column('table', 'column', ...) - helper function pattern
        ensure_col_pattern = re.compile(
            rf"ensure_column\s*\(\s*['\"]({re.escape(table_name)})['\"],\s*['\"](\w+)['\"]",
            re.MULTILINE
        )

        for match in ensure_col_pattern.finditer(search_content):
            if match.group(1) == table_name:
                protected_columns.append(match.group(2))

        return protected_columns


# ============================================================================
# Main Analyzer
# ============================================================================

class BlastRadiusAnalyzer:
    """Main analyzer combining all parsers."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.docker_parser = DockerComposeParser()
        self.migration_parser = MigrationParser()
        self.model_parser = SQLAlchemyModelParser()
        self.sync_checker = SyncChecker()
        self.idempotency_checker = IdempotencyChecker()

    def analyze(self, profile: dict) -> AnalysisResult:
        result = AnalysisResult()

        # 1. Docker Compose
        docker_path = self.project_root / profile.get('infrastructure', 'docker-compose.yml')
        if docker_path.exists():
            result.services = self.docker_parser.parse(str(docker_path))
            result.dependency_graph = self._build_dependency_graph(result.services)

        # 2. Migrations
        migrations_dir = self.project_root / 'alembic' / 'versions'
        if migrations_dir.exists():
            result.db_tables = self.migration_parser.parse_directory(str(migrations_dir))
            # Check for non-idempotent patterns
            result.idempotency_issues = self.idempotency_checker.check_directory(str(migrations_dir))

        # 3. Models
        models_path = self.project_root / 'app' / 'models.py'
        if models_path.exists():
            result.model_fields = self.model_parser.parse_file(str(models_path))

        # 4. Sync check
        result.sync_issues = self.sync_checker.check(result.db_tables, result.model_fields)

        return result

    def _build_dependency_graph(self, services: Dict[str, ServiceNode]) -> Dict[str, List[str]]:
        graph = {}
        for name, service in services.items():
            deps = service.depends_on.copy()

            # Infer DB dependency from environment
            for key in service.environment:
                if 'DATABASE' in key or 'DB_' in key:
                    if 'db' in services and 'db' not in deps:
                        deps.append('db')

            graph[name] = deps
        return graph

    def print_report(self, result: AnalysisResult):
        print("\n" + "=" * 60)
        print("BLAST RADIUS ANALYZER - REPORT")
        print("=" * 60)

        # Services
        print(f"\n[SERVICES] Found {len(result.services)} services:")
        for name, svc in result.services.items():
            deps = ', '.join(svc.depends_on) or 'none'
            ports = ', '.join(str(p) for p in svc.ports) or 'none'
            print(f"  - {name}: ports={ports}, depends_on=[{deps}]")

        # Database
        print(f"\n[DATABASE] Found {len(result.db_tables)} tables in migrations:")
        for name, table in result.db_tables.items():
            print(f"  - {name}: {len(table.columns)} columns")

        # Models
        print(f"\n[MODELS] Found {len(result.model_fields)} models:")
        for name, fields in result.model_fields.items():
            print(f"  - {name}: {len(fields)} fields")

        # Sync Issues
        if result.sync_issues:
            print(f"\n[SYNC ISSUES] Found {len(result.sync_issues)} issues:")
            for issue in result.sync_issues:
                print(f"  [!] {issue}")
        else:
            print("\n[SYNC] All models match migrations")

        # Idempotency Issues
        if result.idempotency_issues:
            print(f"\n[IDEMPOTENCY] Found {len(result.idempotency_issues)} non-idempotent migrations:")
            for issue in result.idempotency_issues:
                print(f"  [!] {issue.file}: {issue.message}")
        else:
            print("\n[IDEMPOTENCY] All migrations are idempotent")

        print("\n" + "=" * 60)


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Blast Radius Analyzer - Detect model/migration sync issues'
    )
    parser.add_argument(
        '--project', '-p',
        required=True,
        help='Path to project root'
    )
    parser.add_argument(
        '--profile', '-f',
        help='Path to project profile JSON (optional)'
    )
    parser.add_argument(
        '--json', '-j',
        action='store_true',
        help='Output as JSON'
    )

    args = parser.parse_args()

    # Default profile
    profile = {
        "project_name": "unknown",
        "infrastructure": "docker-compose.yml"
    }

    if args.profile:
        with open(args.profile, 'r') as f:
            profile = json.load(f)

    analyzer = BlastRadiusAnalyzer(args.project)
    result = analyzer.analyze(profile)

    if args.json:
        output = {
            "services": len(result.services),
            "tables": len(result.db_tables),
            "models": len(result.model_fields),
            "sync_issues": result.sync_issues,
            "idempotency_issues": [
                {
                    "file": issue.file,
                    "table": issue.table,
                    "columns": issue.columns,
                    "message": issue.message
                }
                for issue in result.idempotency_issues
            ]
        }
        print(json.dumps(output, indent=2))
    else:
        analyzer.print_report(result)

    # Exit code based on issues
    has_issues = result.sync_issues or result.idempotency_issues
    exit(1 if has_issues else 0)


if __name__ == '__main__':
    main()
