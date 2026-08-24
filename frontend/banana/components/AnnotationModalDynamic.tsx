"use client";

import dynamic from "next/dynamic";

const AnnotationModal = dynamic(
  () => import("./AnnotationModal").then((mod) => mod.AnnotationModal),
  { ssr: false }
);

export default AnnotationModal;

