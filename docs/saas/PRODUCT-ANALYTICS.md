# Product analytics API and privacy contract

This document defines the adapter contract for
`saas_core.domain.product_analytics.ProductAnalyticsService`. It does not
represent a live public HTTP endpoint.

## Consent

`POST /api/v1/analytics/consent` accepts:

```json
{
  "product_analytics": true,
  "consent_version": "privacy-v1"
}
```

The server derives either the authenticated `user_id` or first-party
`anonymous_id` from its trusted session/cookie boundary. The request body
cannot choose a user ID, role, permission or session identity. The version must
match the currently published privacy notice. A consent withdrawal is stored
as a current false value and an append-only history row.

## Event batches

`POST /api/v1/analytics/events` accepts up to 50 events. Example:

```json
{
  "events": [{
    "event_id": "9a87cb6e-1b3d-4ab6-bfd0-6e2cbdc9d710",
    "event_name": "click",
    "page_path": "/stocks/فولاد",
    "component_id": "stock-shareholder-tab",
    "element_role": "tab",
    "occurred_at": "2026-11-26T18:00:00Z",
    "viewport_width": 1440,
    "viewport_height": 900,
    "metadata": {"action": "open_section"}
  }]
}
```

The server injects `user_id`, `anonymous_id` and `session_id` separately.
The supported event names are `page_view`, `navigation`, `click`,
`scroll_depth`, `section_view`, `active_time` and `product_action`.
Click events require a stable component ID and safe element role. Scroll
percentages are limited to 25, 50, 75, 90 and 100. Active-time records are
capped at 15 minutes per event.

The response is `{"accepted": n, "duplicates": n}`. The event UUID is an
idempotency key; a retried event is not stored twice.

## Metadata allow-list

Metadata is event-specific and consists only of short identifiers, booleans
or bounded integers. Free text is never accepted. The service rejects unknown
keys and fields such as `value`, `text`, `phone`, `email`, `password`,
`otp`, `bank`, `payment` or `message`. Query strings and fragments are
not stored in `page_path`.

## Reporting

- Component heatmap: aggregated clicks, section views and active duration per
  stable page/component ID. It is not a pixel-level pointer heatmap.
- Funnel: ordered completion counts for 2–8 approved event types.
- Cohort retention: weekly aggregated active visitor counts.
- User-level event history: permission `analytics.read` is required and each
  access inserts an immutable audit-log row in the same transaction.

## Retention

Raw event rows are purged after the configured retention period, 365 days by
default. The deletion is permission-checked and audited. Consent history is
retained separately. Production must publish the actual retention duration and
provide the legally required consent/withdrawal experience before collection is
enabled.
