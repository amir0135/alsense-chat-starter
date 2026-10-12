# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Alsense operations users need a quick way to ask questions about the sites and operational guidance
available to them. The current audience is also the team evaluating a synthetic reference experience.

## Product Purpose

This starter demonstrates an operations assistant that answers from approved tool results and prepares
synthetic reports or support requests for a person to review. Success means people can understand the
answer, its evidence, the site scope, and whether an action has actually been submitted.

## Positioning

The backend, not the model, owns data access and tool execution. A replaceable adapter boundary keeps
future service credentials and authorization checks outside model prompts and tool arguments.

## Operating Context

The experience is a browser-based conversation. A user starts a synthetic session, asks a question,
reviews cited operational guidance, and may prepare a report or support request for explicit confirmation.
The optional hosted demo uses Azure AI Foundry with synthetic tools.

## Capabilities and Constraints

Implemented tools cover documentation search, report types, permitted-site lookup, report preparation
and status, and support-request preparation. Local demo mode is deterministic; Foundry mode uses the
configured model with backend-owned tools. Submissions are mock-only. Cloud sessions use replica-local
SQLite and are not durable across app replacement. Real Alsense authentication, APIs, Search, shared
storage, and production authorization are not implemented.

## Brand Commitments

This is an independent reference starter, not an official Alsense integration or a Danfoss/LHU product.
Keep the interface clear, practical, and suitable for an operations workflow without implying a live
connection to Alsense systems.

## Evidence on Hand

All demo identities, sites, alarms, documents, report results, and support submissions are synthetic.
There are no real customer records or production API contracts in the starter.

## Product Principles

- Never invent an operational result when the approved tools have no evidence.
- Keep tenant and site authorization in backend code.
- Show the sources behind an answer.
- Preparing a report or ticket is not submission; require a distinct user confirmation.
- Label synthetic data and mock outcomes clearly.
