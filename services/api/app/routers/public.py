"""Public read API.

Endpoints are *page-shaped*, not resource-shaped: each one returns exactly what a
page needs in a single round trip. Generic REST resources would push the web app into
N+1 API calls (ADR-0006).

Everything here is unauthenticated and cacheable. Nothing here can write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from thedrop_database.enums import ArticleStatus
from thedrop_database.models import Article, Category

from app.deps import SessionDep

router = APIRouter(prefix="/api/v1/public", tags=["public"])

# Published content is immutable enough to cache hard at the edge; the publish task
# revalidates explicitly when something changes.
_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"


def _serialize_summary(article: Article) -> dict[str, Any]:
    hero = article.hero_media
    return {
        "id": str(article.public_id),
        "slug": article.slug,
        "path": article.path,
        "headline": article.headline,
        "dek": article.dek,
        "articleType": article.article_type,
        "category": {
            "slug": article.category.slug,
            "name": article.category.name,
            "description": article.category.description,
            "accentToken": article.category.accent_token,
        },
        "publishedAt": article.published_at.isoformat() if article.published_at else None,
        "updatedAt": (
            article.updated_at_public.isoformat() if article.updated_at_public else None
        ),
        "readingTimeSeconds": article.reading_time_seconds,
        "isSponsored": article.is_sponsored,
        "heroImage": (
            {
                "id": str(hero.public_id),
                "url": f"/media/{hero.storage_key}",
                "width": hero.width,
                "height": hero.height,
                "altText": hero.alt_text,
                "caption": hero.caption,
                "credit": hero.credit,
                "blurhash": hero.blurhash,
                "rightsStatus": hero.rights_status,
                "isAiGenerated": hero.is_ai_generated,
                "aiDisclosure": hero.ai_disclosure_text,
            }
            if hero
            else None
        ),
    }


def _published_query():
    return (
        select(Article)
        .options(selectinload(Article.category), selectinload(Article.hero_media))
        .where(Article.status == ArticleStatus.PUBLISHED, Article.deleted_at.is_(None))
        .order_by(Article.published_at.desc())
    )


@router.get("/categories")
def list_categories(db: SessionDep, response: Response) -> list[dict[str, Any]]:
    response.headers["Cache-Control"] = _CACHE_CONTROL
    categories = db.scalars(
        select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
    ).all()
    return [
        {
            "slug": c.slug,
            "name": c.name,
            "description": c.description,
            "accentToken": c.accent_token,
            "isCommercial": c.is_commercial,
        }
        for c in categories
    ]


@router.get("/articles")
def list_articles(
    db: SessionDep,
    response: Response,
    category: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1, le=500)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = _CACHE_CONTROL

    query = _published_query()
    if category:
        query = query.join(Category).where(Category.slug == category)

    offset = (page - 1) * page_size
    # Fetch one extra to determine hasMore without a second COUNT query.
    rows = db.scalars(query.offset(offset).limit(page_size + 1)).all()
    has_more = len(rows) > page_size
    items = rows[:page_size]

    return {
        "items": [_serialize_summary(a) for a in items],
        "page": page,
        "pageSize": page_size,
        "hasMore": has_more,
        "total": offset + len(items) + (1 if has_more else 0),
    }


@router.get("/articles/{category}/{year}/{month}/{day}/{slug}")
def get_article(
    db: SessionDep,
    response: Response,
    category: str,
    year: int,
    month: int,
    day: int,
    slug: str,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = _CACHE_CONTROL

    article = db.scalar(
        _published_query()
        .options(
            selectinload(Article.source_refs),
            selectinload(Article.corrections),
            selectinload(Article.tags),
        )
        .join(Category)
        .where(Category.slug == category, Article.slug == slug)
    )
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")

    # The date is part of the canonical URL, so a mismatched date is a different URL
    # for the same content -- reject rather than serve duplicate paths.
    published = article.first_published_at
    if published is None or (published.year, published.month, published.day) != (year, month, day):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")

    payload = _serialize_summary(article)
    payload.update(
        {
            "body": article.body_blocks,
            "keyFacts": article.key_facts,
            "byline": article.byline,
            "tags": [{"slug": t.slug, "name": t.name} for t in article.tags],
            "sources": [
                {
                    "publisher": ref.publisher,
                    "title": ref.title,
                    "url": ref.url,
                    "refType": ref.ref_type,
                }
                for ref in sorted(article.source_refs, key=lambda r: r.display_order)
            ],
            "corrections": [
                {
                    "type": c.correction_type,
                    "summary": c.summary,
                    "detail": c.detail,
                    "issuedAt": c.issued_at.isoformat(),
                }
                for c in article.corrections
                if c.is_public
            ],
            "seo": {
                "title": article.seo_title or article.headline,
                "metaDescription": article.meta_description or article.dek,
                "ogTitle": article.og_title or article.headline,
                "ogDescription": article.og_description or article.dek,
                "canonicalUrl": article.canonical_url or article.path,
                "noindex": article.noindex,
            },
            "structuredData": article.structured_data,
            "disclosure": article.disclosure_text,
        }
    )
    return payload


@router.get("/latest")
def latest(db: SessionDep, response: Response, limit: Annotated[int, Query(ge=1, le=50)] = 20):
    response.headers["Cache-Control"] = _CACHE_CONTROL
    rows = db.scalars(_published_query().limit(limit)).all()
    return {
        "items": [_serialize_summary(a) for a in rows],
        "generatedAt": datetime.now(UTC).isoformat(),
    }
