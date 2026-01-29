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
class SchemaDrift:
    """Schema drift between migrations and live database."""
    table: str
    issue_type: str  # 'missing_table', 'extra_table', 'missing_column', 'extra_column', 'type_mismatch'
    details: str


@dataclass
class ForeignKeyDef:
    """Foreign key definition from migration."""
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    migration_file: str
    migration_order: int  # Order in which migration appears


@dataclass
class ForeignKeyIssue:
    """Foreign key consistency issue."""
    issue_type: str  # 'missing_target_table', 'missing_target_column', 'wrong_order'
    source: str  # e.g., 'user_achievements.user_id'
    target: str  # e.g., 'users.id'
    details: str
    migration_file: str


@dataclass
class EnvVarIssue:
    """Environment variable issue."""
    issue_type: str  # 'defined_not_used', 'used_not_defined'
    var_name: str
    service: Optional[str]  # Service where defined (if applicable)
    used_in: List[str]  # Files where used
    details: str


@dataclass
class LiveDBColumn:
    """Column from live database."""
    name: str
    data_type: str
    is_nullable: bool
    column_default: Optional[str] = None


@dataclass
class LiveDBTable:
    """Table from live database."""
    name: str
    columns: Dict[str, LiveDBColumn] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """Complete analysis result."""
    services: Dict[str, ServiceNode] = field(default_factory=dict)
    db_tables: Dict[str, TableSchema] = field(default_factory=dict)
    model_fields: Dict[str, Dict[str, ModelField]] = field(default_factory=dict)
    sync_issues: List[str] = field(default_factory=list)
    idempotency_issues: List[IdempotencyIssue] = field(default_factory=list)
    schema_drift: List[SchemaDrift] = field(default_factory=list)
    fk_issues: List[ForeignKeyIssue] = field(default_factory=list)
    foreign_keys: List[ForeignKeyDef] = field(default_factory=list)
    env_issues: List[EnvVarIssue] = field(default_factory=list)
    env_vars_defined: Dict[str, List[str]] = field(default_factory=dict)  # var -> [services]
    env_vars_used: Dict[str, List[str]] = field(default_factory=dict)  # var -> [files]
    live_db_tables: Dict[str, LiveDBTable] = field(default_factory=dict)
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
# Foreign Key Checker
# ============================================================================

class ForeignKeyChecker:
    """Check foreign key consistency in migrations.

    Detects:
    - FK referencing non-existent tables
    - FK referencing non-existent columns
    - FK defined before target table is created
    """

    def parse_and_check(self, migrations_dir: str) -> tuple:
        """Parse migrations for FK definitions and check consistency.

        Returns:
            tuple: (list of ForeignKeyDef, list of ForeignKeyIssue)
        """
        foreign_keys = []
        migrations_path = Path(migrations_dir)

        if not migrations_path.exists():
            return [], []

        # Build revision chain to get actual migration order
        revision_order = self._build_revision_order(migrations_path)

        # Track table creation order
        table_creation_order = {}  # table_name -> migration_order
        migration_files = list(migrations_path.glob('*.py'))

        # First pass: find all table creations and their order
        for migration_file in migration_files:
            content = migration_file.read_text(encoding='utf-8')
            revision = self._get_revision(content)
            order = revision_order.get(revision, 999)

            tables = self._find_created_tables(content)
            for table in tables:
                if table not in table_creation_order:
                    table_creation_order[table] = order

        # Second pass: find all FK definitions
        for migration_file in migration_files:
            content = migration_file.read_text(encoding='utf-8')
            revision = self._get_revision(content)
            order = revision_order.get(revision, 999)

            fks = self._parse_foreign_keys(content, migration_file.name, order)
            foreign_keys.extend(fks)

        # Check FK consistency
        issues = self._check_consistency(foreign_keys, table_creation_order)

        return foreign_keys, issues

    def _build_revision_order(self, migrations_path: Path) -> Dict[str, int]:
        """Build actual migration order from Alembic revision chain."""
        revisions = {}  # revision -> down_revision
        heads = set()
        all_revisions = set()

        # Parse all revisions
        for migration_file in migrations_path.glob('*.py'):
            content = migration_file.read_text(encoding='utf-8')
            revision = self._get_revision(content)
            down_revision = self._get_down_revision(content)

            if revision:
                revisions[revision] = down_revision
                all_revisions.add(revision)
                if down_revision:
                    heads.discard(down_revision)
                heads.add(revision)

        # Find the base (revision with no down_revision)
        base = None
        for rev, down in revisions.items():
            if down is None:
                base = rev
                break

        # Walk the chain from base to build order
        order = {}
        current = base
        idx = 0

        while current:
            order[current] = idx
            idx += 1
            # Find next revision (one that has current as down_revision)
            next_rev = None
            for rev, down in revisions.items():
                if down == current:
                    next_rev = rev
                    break
            current = next_rev

        return order

    def _get_revision(self, content: str) -> Optional[str]:
        """Extract revision ID from migration content."""
        match = re.search(r"^revision\s*[=:]\s*['\"](\w+)['\"]", content, re.MULTILINE)
        return match.group(1) if match else None

    def _get_down_revision(self, content: str) -> Optional[str]:
        """Extract down_revision from migration content."""
        match = re.search(r"^down_revision\s*[=:][^=]*['\"](\w+)['\"]", content, re.MULTILINE)
        return match.group(1) if match else None

    def _find_created_tables(self, content: str) -> List[str]:
        """Find all tables created in a migration."""
        tables = []

        # Pattern: op.create_table('table_name', ...)
        create_pattern = re.compile(r"op\.create_table\s*\(\s*['\"](\w+)['\"]")
        for match in create_pattern.finditer(content):
            tables.append(match.group(1))

        return tables

    def _parse_foreign_keys(self, content: str, filename: str, order: int) -> List[ForeignKeyDef]:
        """Parse FK definitions from migration content."""
        foreign_keys = []

        # Pattern 1: sa.ForeignKey('table.column') or sa.ForeignKey('table.column', ...)
        fk_pattern = re.compile(
            r"sa\.ForeignKey\s*\(\s*['\"](\w+)\.(\w+)['\"]",
            re.MULTILINE
        )

        # We need to find the context (which column this FK is on)
        # Pattern: sa.Column('column_name', ..., sa.ForeignKey('table.column'))
        column_fk_pattern = re.compile(
            r"sa\.Column\s*\(\s*['\"](\w+)['\"].*?"
            r"sa\.ForeignKey\s*\(\s*['\"](\w+)\.(\w+)['\"]",
            re.DOTALL
        )

        # Find the current table context
        # This is tricky - we need to find which table the FK belongs to
        lines = content.split('\n')
        current_table = None

        for i, line in enumerate(lines):
            # Track current table in create_table block
            if 'op.create_table(' in line:
                match = re.search(r"op\.create_table\s*\(\s*['\"](\w+)['\"]", line)
                if match:
                    current_table = match.group(1)
                else:
                    # Table name might be on next line
                    for j in range(i + 1, min(i + 3, len(lines))):
                        match = re.search(r"['\"](\w+)['\"]", lines[j])
                        if match:
                            current_table = match.group(1)
                            break

            # Find FK in this line
            col_match = column_fk_pattern.search(line)
            if col_match and current_table:
                source_column = col_match.group(1)
                target_table = col_match.group(2)
                target_column = col_match.group(3)

                foreign_keys.append(ForeignKeyDef(
                    source_table=current_table,
                    source_column=source_column,
                    target_table=target_table,
                    target_column=target_column,
                    migration_file=filename,
                    migration_order=order
                ))

            # Reset table context at end of create_table
            if current_table and line.strip() == ')':
                # Simple heuristic - might need improvement
                pass

        return foreign_keys

    def _check_consistency(
        self,
        foreign_keys: List[ForeignKeyDef],
        table_creation_order: Dict[str, int]
    ) -> List[ForeignKeyIssue]:
        """Check FK consistency against table definitions."""
        issues = []

        for fk in foreign_keys:
            source = f"{fk.source_table}.{fk.source_column}"
            target = f"{fk.target_table}.{fk.target_column}"

            # Check 1: Target table exists
            if fk.target_table not in table_creation_order:
                issues.append(ForeignKeyIssue(
                    issue_type='missing_target_table',
                    source=source,
                    target=target,
                    details=f"FK '{source}' references table '{fk.target_table}' which is not created in any migration",
                    migration_file=fk.migration_file
                ))
                continue

            # Check 2: Migration order (target table should be created before FK)
            target_order = table_creation_order[fk.target_table]
            if fk.migration_order < target_order:
                issues.append(ForeignKeyIssue(
                    issue_type='wrong_order',
                    source=source,
                    target=target,
                    details=(
                        f"FK '{source}' -> '{target}' defined in migration that runs BEFORE "
                        f"target table '{fk.target_table}' is created"
                    ),
                    migration_file=fk.migration_file
                ))

        return issues


# ============================================================================
# Environment Variable Checker
# ============================================================================

class EnvVarChecker:
    """Check environment variable consistency.

    Detects:
    - Variables defined in docker-compose but not used in code
    - Variables used in code but not defined in docker-compose
    """

    # Patterns to search for env var usage in code
    ENV_PATTERNS = [
        r"os\.environ\.get\s*\(\s*['\"](\w+)['\"]",  # os.environ.get('VAR')
        r"os\.environ\s*\[\s*['\"](\w+)['\"]\s*\]",  # os.environ['VAR']
        r"os\.getenv\s*\(\s*['\"](\w+)['\"]",  # os.getenv('VAR')
        r"process\.env\.(\w+)",  # process.env.VAR (Node.js)
        r"import\.meta\.env\.(\w+)",  # import.meta.env.VAR (Vite)
        r"\$\{(\w+)\}",  # ${VAR} in shell/config
        r"\$(\w+)",  # $VAR in shell (careful with false positives)
    ]

    # Common system/framework vars to ignore
    IGNORE_VARS = {
        'PATH', 'HOME', 'USER', 'SHELL', 'PWD', 'LANG', 'TERM',
        'NODE_ENV', 'MODE', 'DEV', 'PROD', 'BASE_URL', 'SSR',
        'CI', 'DEBUG', 'VERBOSE', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE',
        'TZ', 'LC_ALL', 'LC_CTYPE', 'HOSTNAME', 'TMPDIR',
    }

    # File extensions to search
    CODE_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.sh', '.yaml', '.yml', '.env.example'}

    def __init__(self, project_root: Path):
        self.project_root = project_root

    def check(self, services: Dict[str, ServiceNode]) -> tuple:
        """Check env var consistency.

        Returns:
            tuple: (env_vars_defined, env_vars_used, issues)
        """
        # Collect defined vars from docker-compose
        defined = {}  # var_name -> [services]
        for service_name, service in services.items():
            for var_name in service.environment.keys():
                if var_name not in self.IGNORE_VARS:
                    defined.setdefault(var_name, []).append(service_name)

        # Search for used vars in code
        used = self._find_used_vars()

        # Find issues
        issues = []

        # Vars defined but not used (warning - might be used at runtime)
        for var_name, services_list in defined.items():
            if var_name not in used and not var_name.startswith('_'):
                issues.append(EnvVarIssue(
                    issue_type='defined_not_used',
                    var_name=var_name,
                    service=', '.join(services_list),
                    used_in=[],
                    details=f"'{var_name}' defined in docker-compose ({', '.join(services_list)}) but not found in code"
                ))

        # Vars used but not defined (could be critical)
        for var_name, files in used.items():
            if var_name not in defined and var_name not in self.IGNORE_VARS:
                # Check if it's a VITE_ var (should be defined)
                if var_name.startswith(('VITE_', 'REACT_APP_', 'NEXT_PUBLIC_')):
                    issues.append(EnvVarIssue(
                        issue_type='used_not_defined',
                        var_name=var_name,
                        service=None,
                        used_in=files[:3],  # Limit to 3 files
                        details=f"'{var_name}' used in code but not defined in docker-compose"
                    ))

        return defined, used, issues

    def _find_used_vars(self) -> Dict[str, List[str]]:
        """Find all env vars used in code."""
        used = {}  # var_name -> [files]

        for ext in self.CODE_EXTENSIONS:
            for filepath in self.project_root.rglob(f'*{ext}'):
                # Skip node_modules, venv, etc
                path_str = str(filepath)
                if any(skip in path_str for skip in ['node_modules', 'venv', '.venv', '__pycache__', '.git', 'dist', 'build']):
                    continue

                try:
                    content = filepath.read_text(encoding='utf-8', errors='ignore')
                    vars_in_file = self._extract_vars(content)

                    for var_name in vars_in_file:
                        if var_name not in self.IGNORE_VARS:
                            rel_path = str(filepath.relative_to(self.project_root))
                            used.setdefault(var_name, []).append(rel_path)
                except Exception:
                    continue

        return used

    def _extract_vars(self, content: str) -> set:
        """Extract env var names from content."""
        vars_found = set()

        for pattern in self.ENV_PATTERNS:
            for match in re.finditer(pattern, content):
                var_name = match.group(1)
                # Filter out obvious non-env vars
                if var_name.isupper() or var_name.startswith(('VITE_', 'REACT_', 'NEXT_', 'DATABASE', 'DB_', 'API_', 'SECRET', 'AWS_', 'REDIS_')):
                    vars_found.add(var_name)

        return vars_found


# ============================================================================
# Live Database Checker
# ============================================================================

class LiveSchemaChecker:
    """Check live database schema against migrations.

    Connects to PostgreSQL and compares actual schema with what
    migrations define. Detects drift caused by:
    - Manual DB changes
    - Failed/partial migrations
    - Missing migrations
    """

    # Map PostgreSQL types to SQLAlchemy type names
    PG_TYPE_MAP = {
        'integer': 'Integer',
        'bigint': 'BigInteger',
        'smallint': 'SmallInteger',
        'character varying': 'String',
        'varchar': 'String',
        'text': 'Text',
        'boolean': 'Boolean',
        'timestamp without time zone': 'DateTime',
        'timestamp with time zone': 'DateTime',
        'date': 'Date',
        'json': 'JSON',
        'jsonb': 'JSON',
        'uuid': 'UUID',
        'numeric': 'Numeric',
        'double precision': 'Float',
        'real': 'Float',
    }

    def __init__(self, db_url: str):
        self.db_url = db_url
        self._conn = None

    def connect(self) -> bool:
        """Establish database connection."""
        try:
            import psycopg2
            self._conn = psycopg2.connect(self.db_url)
            return True
        except ImportError:
            print("[!] psycopg2 not installed. Run: pip install psycopg2-binary")
            return False
        except Exception as e:
            print(f"[!] Failed to connect to database: {e}")
            return False

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_live_schema(self, schema: str = 'public') -> Dict[str, LiveDBTable]:
        """Fetch actual schema from database."""
        if not self._conn:
            return {}

        tables = {}
        cursor = self._conn.cursor()

        # Get all tables in schema
        cursor.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
              AND table_name NOT LIKE 'alembic%%'
        """, (schema,))

        for (table_name,) in cursor.fetchall():
            tables[table_name] = LiveDBTable(name=table_name)

            # Get columns for this table
            cursor.execute("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (schema, table_name))

            for col_name, data_type, is_nullable, col_default in cursor.fetchall():
                tables[table_name].columns[col_name] = LiveDBColumn(
                    name=col_name,
                    data_type=data_type,
                    is_nullable=(is_nullable == 'YES'),
                    column_default=col_default
                )

        cursor.close()
        return tables

    def compare_schemas(
        self,
        migration_tables: Dict[str, TableSchema],
        live_tables: Dict[str, LiveDBTable]
    ) -> List[SchemaDrift]:
        """Compare migration schema with live database."""
        issues = []

        # Check tables defined in migrations
        for table_name, migration_table in migration_tables.items():
            if table_name not in live_tables:
                issues.append(SchemaDrift(
                    table=table_name,
                    issue_type='missing_table',
                    details=f"Table '{table_name}' defined in migrations but missing in database"
                ))
                continue

            live_table = live_tables[table_name]

            # Check columns
            for col_name, migration_col in migration_table.columns.items():
                if col_name not in live_table.columns:
                    issues.append(SchemaDrift(
                        table=table_name,
                        issue_type='missing_column',
                        details=f"Column '{table_name}.{col_name}' defined in migrations but missing in database"
                    ))
                else:
                    # Check type compatibility (optional, can be noisy)
                    live_col = live_table.columns[col_name]
                    expected_type = self._normalize_type(migration_col.type)
                    actual_type = self._normalize_pg_type(live_col.data_type)

                    if expected_type and actual_type and expected_type != actual_type:
                        issues.append(SchemaDrift(
                            table=table_name,
                            issue_type='type_mismatch',
                            details=(
                                f"Column '{table_name}.{col_name}' type mismatch: "
                                f"migrations={migration_col.type}, database={live_col.data_type}"
                            )
                        ))

        # Check for extra tables in database (not in migrations)
        system_tables = {'alembic_version', 'spatial_ref_sys'}
        for table_name in live_tables:
            if table_name not in migration_tables and table_name not in system_tables:
                issues.append(SchemaDrift(
                    table=table_name,
                    issue_type='extra_table',
                    details=f"Table '{table_name}' exists in database but not defined in migrations"
                ))

        # Check for extra columns in database
        for table_name, live_table in live_tables.items():
            if table_name not in migration_tables:
                continue

            migration_cols = migration_tables[table_name].columns
            for col_name in live_table.columns:
                if col_name not in migration_cols and col_name != 'id':
                    issues.append(SchemaDrift(
                        table=table_name,
                        issue_type='extra_column',
                        details=f"Column '{table_name}.{col_name}' exists in database but not in migrations"
                    ))

        return issues

    def _normalize_type(self, sa_type: str) -> Optional[str]:
        """Normalize SQLAlchemy type name."""
        return sa_type.lower() if sa_type else None

    def _normalize_pg_type(self, pg_type: str) -> Optional[str]:
        """Convert PostgreSQL type to normalized name."""
        pg_type_lower = pg_type.lower()
        mapped = self.PG_TYPE_MAP.get(pg_type_lower)
        return mapped.lower() if mapped else pg_type_lower


# ============================================================================
# Main Analyzer
# ============================================================================

class BlastRadiusAnalyzer:
    """Main analyzer combining all parsers."""

    def __init__(self, project_root: str, db_url: Optional[str] = None):
        self.project_root = Path(project_root)
        self.db_url = db_url
        self.docker_parser = DockerComposeParser()
        self.migration_parser = MigrationParser()
        self.model_parser = SQLAlchemyModelParser()
        self.sync_checker = SyncChecker()
        self.idempotency_checker = IdempotencyChecker()
        self.fk_checker = ForeignKeyChecker()
        self.env_checker = EnvVarChecker(self.project_root)
        self.live_schema_checker = LiveSchemaChecker(db_url) if db_url else None

    def analyze(self, profile: dict) -> AnalysisResult:
        result = AnalysisResult()

        # 1. Docker Compose
        docker_path = self.project_root / profile.get('infrastructure', 'docker-compose.yml')
        if docker_path.exists():
            result.services = self.docker_parser.parse(str(docker_path))
            result.dependency_graph = self._build_dependency_graph(result.services)
            # Check env var consistency
            result.env_vars_defined, result.env_vars_used, result.env_issues = self.env_checker.check(result.services)

        # 2. Migrations
        migrations_dir = self.project_root / 'alembic' / 'versions'
        if migrations_dir.exists():
            result.db_tables = self.migration_parser.parse_directory(str(migrations_dir))
            # Check for non-idempotent patterns
            result.idempotency_issues = self.idempotency_checker.check_directory(str(migrations_dir))
            # Check FK consistency
            result.foreign_keys, result.fk_issues = self.fk_checker.parse_and_check(str(migrations_dir))

        # 3. Models
        models_path = self.project_root / 'app' / 'models.py'
        if models_path.exists():
            result.model_fields = self.model_parser.parse_file(str(models_path))

        # 4. Sync check (models vs migrations)
        result.sync_issues = self.sync_checker.check(result.db_tables, result.model_fields)

        # 5. Live DB check (migrations vs actual database)
        if self.live_schema_checker and self.live_schema_checker.connect():
            try:
                # Get schema from profile or default to 'public'
                db_schema = profile.get('db_schema', 'public')
                result.live_db_tables = self.live_schema_checker.get_live_schema(db_schema)
                result.schema_drift = self.live_schema_checker.compare_schemas(
                    result.db_tables,
                    result.live_db_tables
                )
            finally:
                self.live_schema_checker.close()

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

        # Foreign Key Issues
        if result.foreign_keys:
            print(f"\n[FOREIGN KEYS] Found {len(result.foreign_keys)} FK definitions")
            if result.fk_issues:
                print(f"\n[FK ISSUES] Found {len(result.fk_issues)} issues:")
                for issue in result.fk_issues:
                    severity = "[CRITICAL]" if issue.issue_type in ('missing_target_table', 'wrong_order') else "[WARNING]"
                    print(f"  {severity} {issue.details}")
                    print(f"           File: {issue.migration_file}")
            else:
                print("[FK] All foreign keys are consistent")

        # Environment Variables
        if result.env_vars_defined or result.env_vars_used:
            print(f"\n[ENV VARS] {len(result.env_vars_defined)} defined, {len(result.env_vars_used)} used in code")
            if result.env_issues:
                print(f"\n[ENV ISSUES] Found {len(result.env_issues)} issues:")
                for issue in result.env_issues:
                    severity = "[WARNING]" if issue.issue_type == 'defined_not_used' else "[INFO]"
                    print(f"  {severity} {issue.details}")
                    if issue.used_in:
                        print(f"           Used in: {', '.join(issue.used_in)}")
            else:
                print("[ENV] All env vars are consistent")

        # Schema Drift (Live DB)
        if result.live_db_tables:
            print(f"\n[LIVE DB] Connected, found {len(result.live_db_tables)} tables")
            if result.schema_drift:
                print(f"\n[SCHEMA DRIFT] Found {len(result.schema_drift)} issues:")
                # Group by issue type
                by_type = {}
                for drift in result.schema_drift:
                    by_type.setdefault(drift.issue_type, []).append(drift)

                type_labels = {
                    'missing_table': '[CRITICAL] MISSING TABLES (in migrations, not in DB)',
                    'missing_column': '[CRITICAL] MISSING COLUMNS (in migrations, not in DB)',
                    'extra_table': '[WARNING] EXTRA TABLES (in DB, not in migrations)',
                    'extra_column': '[WARNING] EXTRA COLUMNS (in DB, not in migrations)',
                    'type_mismatch': '[INFO] TYPE MISMATCHES',
                }

                for issue_type, label in type_labels.items():
                    if issue_type in by_type:
                        print(f"\n  {label}:")
                        for drift in by_type[issue_type]:
                            print(f"    - {drift.details}")
            else:
                print("\n[SCHEMA DRIFT] Database matches migrations ✓")
        elif self.db_url:
            print("\n[LIVE DB] Failed to connect to database")

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
        '--db-url', '-d',
        help='PostgreSQL connection URL (e.g., postgresql://user:pass@host:5432/db)'
    )
    parser.add_argument(
        '--json', '-j',
        action='store_true',
        help='Output as JSON'
    )

    args = parser.parse_args()

    # Also check DATABASE_URL environment variable
    db_url = args.db_url or os.environ.get('DATABASE_URL')

    # Default profile
    profile = {
        "project_name": "unknown",
        "infrastructure": "docker-compose.yml"
    }

    if args.profile:
        with open(args.profile, 'r') as f:
            profile = json.load(f)

    analyzer = BlastRadiusAnalyzer(args.project, db_url=db_url)
    result = analyzer.analyze(profile)

    if args.json:
        output = {
            "services": len(result.services),
            "tables_in_migrations": len(result.db_tables),
            "tables_in_database": len(result.live_db_tables),
            "models": len(result.model_fields),
            "foreign_keys": len(result.foreign_keys),
            "sync_issues": result.sync_issues,
            "idempotency_issues": [
                {
                    "file": issue.file,
                    "table": issue.table,
                    "columns": issue.columns,
                    "message": issue.message
                }
                for issue in result.idempotency_issues
            ],
            "fk_issues": [
                {
                    "issue_type": issue.issue_type,
                    "source": issue.source,
                    "target": issue.target,
                    "details": issue.details,
                    "file": issue.migration_file
                }
                for issue in result.fk_issues
            ],
            "env_vars_defined": len(result.env_vars_defined),
            "env_vars_used": len(result.env_vars_used),
            "env_issues": [
                {
                    "issue_type": issue.issue_type,
                    "var_name": issue.var_name,
                    "service": issue.service,
                    "used_in": issue.used_in,
                    "details": issue.details
                }
                for issue in result.env_issues
            ],
            "schema_drift": [
                {
                    "table": drift.table,
                    "issue_type": drift.issue_type,
                    "details": drift.details
                }
                for drift in result.schema_drift
            ]
        }
        print(json.dumps(output, indent=2))
    else:
        analyzer.print_report(result)

    # Exit code based on issues
    critical_drift = [d for d in result.schema_drift if d.issue_type in ('missing_table', 'missing_column')]
    critical_fk = [f for f in result.fk_issues if f.issue_type in ('missing_target_table', 'wrong_order')]
    has_issues = result.sync_issues or result.idempotency_issues or critical_drift or critical_fk
    exit(1 if has_issues else 0)


if __name__ == '__main__':
    main()
