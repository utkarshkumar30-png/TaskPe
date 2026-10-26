# Legacy business campaign handling

Database initialization performs an additive, repeatable quarantine for legacy
rows that are still active, have `campaign_review_status = 'not_required'`,
and have an owner whose persisted `users.role` is `business`. Those rows are
changed to `status = 'pending_review'` and `campaign_review_status =
'pending_review'`.

The migration does not infer or create consent declarations, recipient
fingerprints, administrator identities, or approval timestamps. A campaign
without a valid declaration for its current normalized recipient snapshot
therefore remains blocked; the business owner must complete the legitimate
declaration flow before an administrator can approve it. Non-business tasks
and admin-created tasks are not classified by this path.

Repeated initialization finds no further matching rows and does not change
already reviewed campaigns. Run this migration only against a local database
copy during this release phase; no staging or production migration is part of
the implementation.
