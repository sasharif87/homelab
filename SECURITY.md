# Security Policy

## What This Repo Is

This is a collection of Docker Compose configs, automation scripts, and documentation for a self-hosted home server. It's not a software product — there are no releases, no versioned APIs, and no user accounts.

The configs are sanitized for public sharing: all IPs, domains, usernames, and API keys have been replaced with environment variables. If something slipped through, that's worth knowing about.

## Reporting a Leak

If you find a hardcoded credential, personal IP, API key, or any other sensitive information that shouldn't be public — please don't open a public issue. Use GitHub's private vulnerability reporting instead (Security → Report a vulnerability), or email directly.

It'll get addressed quickly.

## Scope

**In scope:**
- Hardcoded credentials or personal info that made it into the public branch
- A config pattern that would expose a service insecurely if followed as written
- A script that does something dangerous without making it obvious

**Out of scope:**
- Vulnerabilities in the upstream software these configs deploy (report those to the upstream project)
- Your own deployment — these configs are examples, not a managed service. You're responsible for your own environment.

## A Note on the Configs

Nothing here should be copied verbatim without reading it. Every config assumes you've set your own values in `.env`. The `.env.example` documents what's expected — see [compose/.env.example](compose/.env.example).
