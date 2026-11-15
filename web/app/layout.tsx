import type { Metadata } from "next";
import "./globals.css";
import Providers from "./components/Providers";
import Shell from "./components/Shell";

export const metadata: Metadata = {
  title: "Visci",
  description: "Generate a vertical film from a single idea.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;700&display=swap"
        />
      </head>
      <body>
        {/* Providers must wrap Shell, because Shell itself uses a query for
            the film list. Anything calling useQuery has to be inside this. */}
        <Providers>
          <Shell>{children}</Shell>
        </Providers>
      </body>
    </html>
  );
}
