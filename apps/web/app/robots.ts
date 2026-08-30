import type { MetadataRoute } from "next";

import { SITE } from "@thedrop/config";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        // Search results and the preview path are thin or private. Excluding them is
        // part of not creating low-value indexable pages.
        disallow: ["/admin", "/api/", "/search", "/preview/", "/go/"],
      },
    ],
    sitemap: [`${SITE.url}/sitemap.xml`],
    host: SITE.url,
  };
}
