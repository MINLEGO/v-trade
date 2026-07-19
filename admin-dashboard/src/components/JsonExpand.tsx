import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ChevronDown, ChevronUp } from "lucide-react";

interface JsonExpandProps {
  data: unknown;
  label?: string;
}

export function JsonExpand({ data, label }: JsonExpandProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setExpanded(!expanded)}
        className="h-auto p-0 text-xs text-muted-foreground hover:text-[#8fd3ff]"
      >
        {expanded ? (
          <ChevronUp className="mr-1 size-3" />
        ) : (
          <ChevronDown className="mr-1 size-3" />
        )}
        {label ?? "Show details"}
      </Button>
      {expanded && (
        <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-[#0d1117] p-3 text-xs text-[#eef3f8]">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}
