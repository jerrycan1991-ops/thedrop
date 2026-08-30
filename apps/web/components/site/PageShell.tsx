/** Shared shell for policy and information pages. Keeps measure and rhythm consistent. */
export function PageShell({
  title,
  intro,
  updated,
  children,
}: {
  title: string;
  intro?: string;
  updated?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto max-w-[--content-width] px-4 py-12 sm:px-6">
      <h1 className="display text-4xl">{title}</h1>
      {intro && <p className="dek mt-3 text-lg">{intro}</p>}
      {updated && <p className="meta mt-4">Last updated {updated}</p>}
      <div className="prose-drop mt-8">{children}</div>
    </div>
  );
}
