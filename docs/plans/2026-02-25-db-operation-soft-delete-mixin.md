# Apply @db_operation and SoftDeleteMixin Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace manual try/except boilerplate and soft-delete patterns across all agent_db.py methods with standardized decorators and mixins.

**Architecture:** Create `@db_operation` decorator in `base_alchemy_db.py` for consistent error handling. Create `SoftDeleteMixin` in `models.py` to standardize soft-delete operations. Apply both across all ~206 methods in `agent_db.py`.

**Tech Stack:** Python, SQLAlchemy, structlog

---

### Task 1: Create `@db_operation` decorator

**Files:**
- Modify: `skyvern/forge/sdk/db/base_alchemy_db.py`

The decorator:
- Passes through application exceptions (`NotFoundError`, `SkyvernException`, `ValueError`)
- Catches `SQLAlchemyError` -> `LOG.exception` -> re-raise
- Catches `Exception` -> `LOG.exception` -> re-raise
- Uses `LOG.exception` consistently (not `LOG.error` with `exc_info=True`)

### Task 2: Create `SoftDeleteMixin`

**Files:**
- Modify: `skyvern/forge/sdk/db/models.py`

Add mixin class with:
- `exclude_deleted(cls, query)` classmethod - adds `filter(cls.deleted_at.is_(None))`
- `mark_deleted(self)` instance method - sets `self.deleted_at = datetime.utcnow()`
- `soft_delete_values(cls)` classmethod - returns `{"deleted_at": datetime.utcnow()}` for bulk updates

Apply to all models with `deleted_at` column (~22 models).

### Task 3: Apply to agent_db.py

**Files:**
- Modify: `skyvern/forge/sdk/db/agent_db.py`

For each method:
1. Add `@db_operation("method_name")` decorator
2. Remove manual try/except boilerplate
3. Replace `Model.deleted_at.is_(None)` with `Model.exclude_deleted(query)`
4. Replace `instance.deleted_at = datetime.utcnow()` with `instance.mark_deleted()`
5. Replace `.values(deleted_at=datetime.utcnow())` with `.values(**Model.soft_delete_values())`

### Task 4: Update tests

Run existing tests to verify no regressions. Add tests for `@db_operation` decorator.

### Task 5: Verify

Run linting, type checking, and all tests.
