<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Creating a new revision](#creating-a-new-revision)
- [Running migrations](#running-migrations)
- [Downgrading migrations](#downgrading-migrations)
- [Check your current alembic setup](#check-your-current-alembic-setup)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Creating a new revision
```
alembic revision --autogenerate -m "enter description here"
```
**Note:** Please read [What does Autogenerate Detect (and what does it not detect?)](https://alembic.sqlalchemy.org/en/latest/autogenerate.html#what-does-autogenerate-detect-and-what-does-it-not-detect) and always make sure to review the generated revision file before running it.

# Running migrations
```
alembic upgrade head
```
# Downgrading migrations
```
alembic downgrade -1
```

# Check your current alembic setup
```
alembic current
```
