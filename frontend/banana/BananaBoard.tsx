"use client";

import { useEffect } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { WorkflowCanvas } from "@/banana/components/WorkflowCanvas";
import { FloatingActionBar } from "@/banana/components/FloatingActionBar";
import AnnotationModal from "@/banana/components/AnnotationModalDynamic";
import { Toast } from "@/banana/components/Toast";
import { ensureBoardOwner } from "@/banana/store/workflowStore";
import { getUserIdFromToken } from "@/lib/authFetch";

export default function BananaBoard() {
  // Смена аккаунта в том же SPA-сеансе: холст прошлого юзера не должен
  // оставаться на экране у нового («открыл в одном аккаунте — открылось
  // и в другом»). Store — модульный синглтон, сбрасываем по владельцу.
  useEffect(() => {
    ensureBoardOwner(getUserIdFromToken());
  });
  return (
    <ReactFlowProvider>
      <div className="h-full w-full bg-brain-950 relative">
        <WorkflowCanvas />
        <FloatingActionBar />
        <AnnotationModal />
        <Toast />
      </div>
    </ReactFlowProvider>
  );
}


