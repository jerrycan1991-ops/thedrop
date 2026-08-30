import Image from "next/image";

import { AdSlot } from "@/components/ads/AdSlot";

/**
 * Renders an article from its block list.
 *
 * This is a security boundary, not just a layout choice. The body arrives as typed
 * blocks and every one is rendered as React children — there is no
 * `dangerouslySetInnerHTML` path for generated content anywhere in this component.
 * A prompt injection that produces `<script>` in a paragraph renders as visible text,
 * because text is all a paragraph block can ever be.
 *
 * Unknown block kinds are dropped silently rather than rendered as JSON: a schema
 * change should degrade to missing content, never to a leaked internal object.
 */

type Block =
  | { kind: "paragraph"; text: string }
  | { kind: "heading"; level: 2 | 3; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "quote"; text: string; attribution: string | null; sourceUrl: string | null }
  | { kind: "image"; asset: ImageAsset }
  | { kind: "keyFacts"; items: string[] }
  | { kind: "table"; headers: string[]; rows: string[][] }
  | { kind: "adSlot"; placement: string }
  | { kind: string; [key: string]: unknown };

interface ImageAsset {
  url: string;
  width: number;
  height: number;
  altText: string;
  caption: string | null;
  credit: string | null;
  isAiGenerated: boolean;
  aiDisclosure: string | null;
}

export function ArticleBody({
  blocks,
  riskTier = "standard",
}: {
  blocks: unknown[];
  riskTier?: string;
}) {
  return (
    <div className="prose-drop">
      {(blocks as Block[]).map((block, index) => {
        switch (block.kind) {
          case "paragraph":
            return <p key={index}>{String(block.text ?? "")}</p>;

          case "heading": {
            const level = block.level === 3 ? 3 : 2;
            const Tag = level === 3 ? "h3" : "h2";
            return <Tag key={index}>{String(block.text ?? "")}</Tag>;
          }

          case "list": {
            const items = Array.isArray(block.items) ? (block.items as string[]) : [];
            return block.ordered ? (
              <ol key={index}>
                {items.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ol>
            ) : (
              <ul key={index}>
                {items.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>
            );
          }

          case "quote":
            return (
              <blockquote key={index}>
                <p>{String(block.text ?? "")}</p>
                {block.attribution ? (
                  <cite className="mt-2 block text-sm not-italic text-muted">
                    — {String(block.attribution)}
                  </cite>
                ) : null}
              </blockquote>
            );

          case "image": {
            const asset = block.asset as ImageAsset | undefined;
            if (!asset?.url) return null;
            return (
              <figure key={index} className="my-8">
                <div className="relative overflow-hidden rounded-lg bg-surface">
                  <Image
                    src={asset.url}
                    alt={asset.altText}
                    width={asset.width}
                    height={asset.height}
                    sizes="(max-width: 768px) 100vw, 700px"
                    className="h-auto w-full object-cover"
                  />
                </div>
                <figcaption className="mt-2 text-xs text-subtle">
                  {asset.caption}
                  {asset.credit ? ` (${asset.credit})` : ""}
                  {asset.isAiGenerated ? (
                    <span className="ml-2 font-semibold uppercase tracking-wide">
                      {asset.aiDisclosure ?? "AI-generated illustration"}
                    </span>
                  ) : null}
                </figcaption>
              </figure>
            );
          }

          case "keyFacts": {
            const items = Array.isArray(block.items) ? (block.items as string[]) : [];
            return (
              <aside key={index} className="my-8 rounded-lg border border-line bg-surface p-5">
                <h2 className="meta mb-3">Key facts</h2>
                <ul className="space-y-2 text-base">
                  {items.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              </aside>
            );
          }

          case "table": {
            const headers = Array.isArray(block.headers) ? (block.headers as string[]) : [];
            const rows = Array.isArray(block.rows) ? (block.rows as string[][]) : [];
            return (
              // Wide tables scroll inside themselves; the page never scrolls sideways.
              <div key={index} className="scroll-x my-8 rounded-lg border border-line">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="bg-sunken">
                      {headers.map((header, i) => (
                        <th key={i} className="border-b border-line px-3 py-2 text-left font-semibold">
                          {header}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, i) => (
                      <tr key={i} className="border-b border-line last:border-0">
                        {row.map((cell, j) => (
                          <td key={j} className="px-3 py-2 align-top">
                            {cell}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          }

          case "adSlot":
            return (
              <AdSlot
                key={index}
                placement="mid_article"
                riskTier={riskTier}
                className="my-8"
              />
            );

          default:
            return null;
        }
      })}
    </div>
  );
}
