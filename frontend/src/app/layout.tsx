import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RoadTwin — Indian road digital twin",
  description:
    "Automated high-fidelity road network modelling and traffic simulation for Indian roads.",
};

// Deliberately no next/font/google: it fetches at build time, and a hackathon
// demo must build and run with no network. A system stack looks native anyway.
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
