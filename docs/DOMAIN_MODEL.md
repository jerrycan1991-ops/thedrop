# Domain model — source-of-truth hierarchy

Established in Phase 1 of the Node-first migration. Two concepts that look similar are
deliberately governed in opposite ways, because they *are* different: one is business
data, the other is application logic.

| | Categories | Article types |
|---|---|---|
| Nature | Runtime business data | Closed set the code switches on |
| Authority | The `categories` **table** | `article_types.json` |
| Add one by | Inserting a row | Editing the JSON + a code review |
| Requires a deploy | **No** | Yes, deliberately |
| Hardcoded in TypeScript | **Never** | Never — imported from the JSON |
| Hardcoded in Python | **Never** | Never — loaded from the JSON |

---

## Categories — the database is authoritative

```
                    categories TABLE          <-- authoritative
                          |
              +-----------+-----------+
              |                       |
   GET /api/v1/public/categories   Python reads rows
              |                       (no hardcoded list)
   apps/web/lib/categories.ts
              |
   header, footer, homepage modules,
   sitemap, generateStaticParams
```

**Rules**

1. No TypeScript file contains a category list. A regression test asserts that
   `packages/config/src/index.ts` exports neither `CATEGORIES` nor `CATEGORY_SLUGS`.
2. `seed.py` defines *initial* categories only. It bootstraps an empty database and is
   never imported by application code — a test asserts that too. Editing it does not
   change existing rows; the seed is idempotent.
3. The web app reads categories through `apps/web/lib/categories.ts`, which wraps the
   public API in React `cache()` so a page rendering categories in the header, the
   footer and a module fetches them once.
4. Adding a category is a row. Once the admin CRUD lands (Phase 3) it is a form.

**Degradation.** If the API is unreachable the category list comes back empty and the
navigation falls back to its static links. A missing section for one render is
recoverable; a 500 on every page is not. There is deliberately no hardcoded fallback
list — that would be the duplication this phase removed.

**Build-time.** `generateStaticParams` pre-renders whatever categories exist at build
time. `dynamicParams` stays at its default `true`, so a category added later still
renders on demand without a deploy. If the API is down during a build, nothing is
pre-rendered, every category page renders on demand, and the build still succeeds.

### Two different "unknown category" behaviours — both intentional

| Surface | Unknown slug | Why |
|---|---|---|
| `GET /api/v1/public/articles?category=nope` | **200, empty list** | A query parameter is a *filter*. Filtering by something that matches nothing yields nothing. |
| `GET /nope` (category page) | **404** | A path segment is a *lookup*. There is no such page. |

Both are pinned: the API behaviour by the Phase 0 baseline, the page behaviour by
`notFound()` in `app/(public)/[category]/page.tsx`.

---

## Article types — one version-controlled file

```
   packages/config/src/thedrop_config/article_types.json   <-- canonical
                     |
        +------------+------------+
        |                         |
   article_types.py          index.ts
   (Python, import-time)     (TypeScript, compile-time)
        |                         |
   commercial_forbidden_sql()   ARTICLE_TYPES, ArticleType,
        |                       EDITORIAL_ARTICLE_TYPES
   affiliate CHECK constraint
        |
   PostgreSQL enforcement
```

**Why not a database table.** Article types are a closed set that business logic
branches on: `NEWS` forbids opinion, `OPINION` requires a label, four of them forbid
affiliate links entirely. A type added by an admin at runtime would be a type no code
knows how to handle. They belong in version control, behind code review.

**Why JSON rather than code generation.** Both languages can read JSON natively —
TypeScript with `resolveJsonModule`, Python with `json.load`. There is no build step, no
generated file to keep in sync, and no way for the two to disagree. Code generation
would add a pipeline to solve a problem that a shared file already solves.

**Why the file lives inside the Python package.** `thedrop_config/article_types.json`
is present in both an editable install and a built wheel with no path guessing. The
TypeScript side imports it by relative path from `packages/config/src/index.ts`. One
file, two readers.

**Order is load-bearing.** The types flagged `forbidsCommercial` are rendered, in
declaration order, into the affiliate CHECK constraint:

```sql
article_type NOT IN ('NEWS', 'ANALYSIS', 'OPINION', 'COMMENTARY')
```

This must stay byte-identical to what revision `bf45495a0cae` applied, or Alembic
reports drift. Two tests guard it, and `alembic check` confirms the database agrees.

**Adding an article type**

1. Add it to `article_types.json`.
2. If it sets `forbidsCommercial: true`, the CHECK expression changes — generate an
   Alembic migration and apply it. Alembic remains the only schema authority.
3. TypeScript picks it up on the next typecheck; Python on the next import.
4. Nothing else to edit. There is no second list.

---

## Enforcement layers

Defence in depth, not redundancy — each layer catches what the one above cannot:

| Layer | Enforces |
|---|---|
| `article_types.json` | The canonical set |
| TypeScript types | Compile-time misuse in the web app |
| Python constants | Runtime validation and defaults |
| Database CHECK | Commercial content cannot attach to an editorial article, whatever the application does |
| Tests | That all four of the above still agree |

---

## Known remaining duplication

Honest list, out of Phase 1 scope:

- **Ad placements** are defined in `AD_PLACEMENTS` (TypeScript), `AD_SLOTS` (`seed.py`)
  and the `ad_placements` table. Same shape of problem as categories, and it should be
  resolved the same way — the table is authoritative — when the ad system is
  implemented in Phase 5.
- **`CATEGORY_SLUGS` in `infrastructure/scripts/api_baseline.py`** is intentional. It is
  a test fixture that pins Phase 0 behaviour; a baseline that read from the database
  would move whenever the database moved, which defeats its purpose.
