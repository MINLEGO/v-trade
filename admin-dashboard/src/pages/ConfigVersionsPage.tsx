import { useState } from "react";
import { useConfigVersions } from "@/api/hooks/useConfigVersions";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/StatusBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { JsonExpand } from "@/components/JsonExpand";
import { ChevronLeft, ChevronRight, ChevronDown, ChevronUp, Layers, FileText } from "lucide-react";
import type { ConfigVersionRow } from "@/api/types";

const LIMIT = 10;

function truncate(str: string, len = 12): string {
  return str.length > len ? `${str.slice(0, len)}…` : str;
}

function ConfigVersionCard({ row }: { row: ConfigVersionRow }) {
  const [showModels, setShowModels] = useState(false);
  const [showPrompts, setShowPrompts] = useState(false);

  return (
    <Card className="border-[#1e2a38] bg-[#18202b]">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-lg font-semibold text-[#eef3f8]">
            {row.experiment_version}
          </span>
          <StatusBadge status={row.status} />
          <Badge
            variant="outline"
            className="bg-[#8fd3ff]/10 text-[#8fd3ff] border-[#8fd3ff]/20"
          >
            v{row.version_number}
          </Badge>
          <span className="font-mono text-xs text-muted-foreground">
            SHA: {truncate(row.config_sha256)}
          </span>
          {row.code_version && (
            <span className="text-xs text-muted-foreground">
              Code: {row.code_version}
            </span>
          )}
          <span className="text-xs text-muted-foreground">
            <RelativeTime date={row.created_at} />
          </span>
          {row.supersedes_id && (
            <span className="text-xs text-muted-foreground">
              Supersedes: {truncate(row.supersedes_id)}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowModels(!showModels)}
            className="text-xs"
          >
            <Layers className="mr-1 size-3" />
            {showModels ? "Hide" : "Show"} models
            {showModels ? (
              <ChevronUp className="ml-1 size-3" />
            ) : (
              <ChevronDown className="ml-1 size-3" />
            )}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowPrompts(!showPrompts)}
            className="text-xs"
          >
            <FileText className="mr-1 size-3" />
            {showPrompts ? "Hide" : "Show"} prompts
            {showPrompts ? (
              <ChevronUp className="ml-1 size-3" />
            ) : (
              <ChevronDown className="ml-1 size-3" />
            )}
          </Button>
        </div>

        {showModels && row.models && row.models.length > 0 && (
          <div className="rounded-md border border-[#1e2a38]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Label</TableHead>
                  <TableHead>Model Slug</TableHead>
                  <TableHead>Provider Policy</TableHead>
                  <TableHead>Parameters</TableHead>
                  <TableHead>Config SHA256</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {row.models.map((model) => (
                  <TableRow key={model.model_config_id}>
                    <TableCell>{model.label}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {model.model_slug}
                    </TableCell>
                    <TableCell>{model.provider_policy}</TableCell>
                    <TableCell>
                      <JsonExpand data={model.parameters} label="Params" />
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {truncate(model.config_sha256)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {showPrompts && row.prompts && row.prompts.length > 0 && (
          <div className="rounded-md border border-[#1e2a38]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Classification</TableHead>
                  <TableHead>Body SHA256</TableHead>
                  <TableHead>Body</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {row.prompts.map((prompt) => (
                  <TableRow key={prompt.prompt_version_id}>
                    <TableCell>{prompt.name}</TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className="bg-[#8fd3ff]/10 text-[#8fd3ff] border-[#8fd3ff]/20 text-xs"
                      >
                        {prompt.classification}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {truncate(prompt.body_sha256)}
                    </TableCell>
                    <TableCell>
                      <details>
                        <summary className="cursor-pointer text-xs text-muted-foreground hover:text-[#8fd3ff]">
                          Show body
                        </summary>
                        <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-[#0d1117] p-3 text-xs text-[#eef3f8] font-mono whitespace-pre-wrap">
                          {prompt.body}
                        </pre>
                      </details>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function ConfigVersionsPage() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useConfigVersions({ limit: LIMIT, offset });

  return (
    <div>
      <PageHeader
        title="Configuration & Versions"
        description="Experiment definitions, model configs, and prompt versions"
      />

      {isLoading ? (
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i} className="border-[#1e2a38] bg-[#18202b]">
              <CardContent className="pt-6">
                <Skeleton className="h-5 w-48" />
                <Skeleton className="mt-2 h-4 w-64" />
                <Skeleton className="mt-2 h-4 w-32" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : data && data.length > 0 ? (
        <div className="space-y-4">
          {data.map((row) => (
            <ConfigVersionCard key={row.id} row={row} />
          ))}

          <div className="flex items-center justify-between px-2">
            <span className="text-sm text-muted-foreground">
              Page {Math.floor(offset / LIMIT) + 1}
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setOffset(Math.max(0, offset - LIMIT))}
                disabled={offset === 0}
              >
                <ChevronLeft className="size-4" />
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setOffset(offset + LIMIT)}
                disabled={data.length < LIMIT}
              >
                Next
                <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex h-24 items-center justify-center text-muted-foreground">
          No configuration versions found.
        </div>
      )}
    </div>
  );
}
