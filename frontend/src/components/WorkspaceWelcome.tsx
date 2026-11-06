import { Plug } from "lucide-react";
import { Link } from "react-router-dom";
import { useConnection } from "../connection";
import { Button } from "@/components/ui/button";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";

export function WorkspaceWelcome() {
  const { openConnection } = useConnection();
  return (
    <section className="connection-empty" aria-label="账号未登录">
      <Empty className="min-h-[min(440px,60dvh)]">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Plug />
          </EmptyMedia>
          <EmptyTitle>登录账号</EmptyTitle>
          <EmptyDescription>
            登录个人账号，查看自己的实验和执行记录。
          </EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button onClick={openConnection}>登录账号</Button>
          <Button
            variant="link"
            nativeButton={false}
            render={<Link to="/guide" />}
          >
            查看接入指南
          </Button>
        </EmptyContent>
      </Empty>
    </section>
  );
}
