# Corrections Caching System

## Overview
The corrections caching system improves performance by avoiding repeated full scans of `categories_custom.json` when looking up correction records. It introduces an in-memory cache that is refreshed only when changes occur.

## Key Components

### 1. Global Cache
- `_CORRECTIONS_CACHE`: a global variable holding a dict mapping normalized descriptions → corrected category/subcategory/type.

### 2. Cache Builder
- `_collect_corrections(tree)`: traverses the custom categories tree and extracts correction records with `corrected_to`, including authoritative and soft corrections.
- `get_corrections_cache()`: returns the cached corrections map, rebuilding it if the cache is empty.

### 3. Cache Invalidation
- `invalidate_corrections_cache()`: clears the cache so the next lookup forces a rebuild.
- Called whenever a new correction is added (`add_correction_or_seed`).

### 4. Usage
- `lookup_correction(description)`: retrieves corrected mapping from cache.
- All categorization updates (`update_categories`) use the cache to apply corrections.

## Workflow
1. User adds a correction.
2. System writes to `categories_custom.json`.
3. `invalidate_corrections_cache()` is called.
4. Next call to `get_corrections_cache()` rebuilds from JSON.
5. Subsequent categorization updates read corrections from memory.

## Benefits
- Faster categorization updates.
- Eliminates repeated disk I/O.
- Ensures consistency (cache is always rebuilt after writes).

## Notes
- Corrections map keys use `description_norm` (normalized description).
- Plain seeds are not cached. Correction records with `corrected_to` are cached whether authoritative or soft; `update_categories()` records the two kinds separately in its result counts.
