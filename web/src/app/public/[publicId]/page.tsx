"use client";

import { useParams } from "next/navigation";
import { PublicGuestPage } from "@/components/PublicGuestPage";

export default function PublicGuestRoute() {
  const { publicId } = useParams<{ publicId: string }>();
  return <PublicGuestPage publicId={publicId} />;
}
