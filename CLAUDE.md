# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Baxter is a scrabble tournament manager built with Django 6.0 and Python 3.14.

## Development Commands

```bash
# Install dependencies
uv sync

# Run development server
uv run python manage.py runserver

# Run migrations
uv run python manage.py migrate

# Create migrations after model changes
uv run python manage.py makemigrations

# Run tests
uv run python manage.py test

# Run a single test
uv run python manage.py test <app_name>.tests.TestClass.test_method

# Django management commands
uv run python manage.py <command>
```

## Configuration

Settings are managed via environment variables using python-decouple. Required variables are stored in `.env` (gitignored):
- `SECRET_KEY` - Django secret key
- `DEBUG` - Boolean, defaults to False
- `ALLOWED_HOSTS` - Comma-separated list of allowed hosts

## Architecture

Standard Django project structure:
- `baxter/` - Main Django project package (settings, urls, wsgi/asgi)
- `tournaments/` - Tournament management app
- `users/` - User authentication and management app
- `manage.py` - Django CLI entry point
- Database: SQLite (development), configurable for production via DATABASE_URL

## Code Standards

Follow web development best practices:
- Use Django's static files system for CSS/JS (not inline styles)
- Keep templates DRY with inheritance and includes
- Follow Django conventions for project structure
- Use class-based views (ListView, DetailView, CreateView, UpdateView, DeleteView)
- Use mixins for reusable view logic (e.g., permission checks)
