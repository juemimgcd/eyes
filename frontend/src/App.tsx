import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Search, UserRound, LogOut, X } from "lucide-react";
import {
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { Api } from "./api";
import { AccountGate } from "./components/AccountGate";
import type { Account } from "./components/AccountGate";
import { ConnectionContext } from "./connection";
import { useRealtime } from "./hooks/use-realtime";
import { navigation, navigationGroups } from "./navigation";
import { Dialog, ErrorNotice, Eye, Loading } from "./components/ui";
import { Button } from "@/components/ui/button";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

const ExperimentsPage = lazy(() =>
  import("./pages/Experiments").then((module) => ({
    default: module.ExperimentsPage,
  })),
);
const BatchesPage = lazy(() =>
  import("./pages/Batches").then((module) => ({ default: module.BatchesPage })),
);
const BatchPage = lazy(() =>
  import("./pages/Batches").then((module) => ({ default: module.BatchPage })),
);
const ObservationPage = lazy(() =>
  import("./pages/Observation").then((module) => ({
    default: module.ObservationPage,
  })),
);
const ObservationRunPage = lazy(() =>
  import("./pages/Observation").then((module) => ({
    default: module.ObservationRunPage,
  })),
);
const ExperimentPage = lazy(() =>
  import("./pages/Experiment").then((module) => ({
    default: module.ExperimentPage,
  })),
);
const CaseWorkspacePage = lazy(() =>
  import("./pages/CaseWorkspace").then((module) => ({
    default: module.CaseWorkspacePage,
  })),
);
const CatalogPage = lazy(() =>
  import("./pages/Catalog").then((module) => ({ default: module.CatalogPage })),
);
const ComparisonPage = lazy(() =>
  import("./pages/Comparison").then((module) => ({
    default: module.ComparisonPage,
  })),
);
const OperationsPage = lazy(() =>
  import("./pages/Operations").then((module) => ({
    default: module.OperationsPage,
  })),
);
const GuidePage = lazy(() =>
  import("./pages/Guide").then((module) => ({ default: module.GuidePage })),
);
const QuickNavigation = lazy(() =>
  import("./components/QuickNavigation").then((module) => ({
    default: module.QuickNavigation,
  })),
);

export default function App() {
  return (
    <AccountGate>
      {(api, account, onLogout) => (
        <Workspace
          key={account.id}
          api={api}
          account={account}
          onLogout={onLogout}
        />
      )}
    </AccountGate>
  );
}

function Workspace({
  api,
  account,
  onLogout,
}: {
  api: Api;
  account: Account;
  onLogout: () => Promise<void>;
}) {
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [quickOpen, setQuickOpen] = useState(false);
  const queryClient = useQueryClient();
  const expireConnection = useCallback(() => {
    queryClient.clear();
    window.dispatchEvent(new Event("eyes-session-expired"));
  }, [queryClient]);
  const realtime = useRealtime(api, expireConnection);
  const location = useLocation();
  const current = navigation.find((item) =>
    location.pathname.startsWith(item.url),
  );
  const pageName = location.pathname.includes("/cases/")
    ? "执行审阅"
    : current?.text || "Eyes";
  useEffect(() => {
    document.title = `${pageName} · Eyes`;
  }, [pageName]);
  const openConnection = () => setConnectionOpen(true);
  return (
    <ConnectionContext.Provider value={{ api, openConnection }}>
      <TooltipProvider>
        <a className="skip-link" href="#main">
          跳转到主内容
        </a>
        <SidebarProvider
          style={
            {
              "--sidebar-width": "14rem",
              "--sidebar-width-icon": "3.5rem",
            } as CSSProperties
          }
        >
          <WorkspaceShell
            api={api}
            account={account}
            realtime={realtime}
            pageName={pageName}
            openConnection={openConnection}
            quickOpen={quickOpen}
            setQuickOpen={setQuickOpen}
          >
            <Suspense fallback={<Loading label="正在打开页面" />}>
              <Routes>
                <Route path="/" element={<Navigate to="/observe" replace />} />
                <Route path="/observe" element={<ObservationPage />} />
                <Route path="/observe/:id" element={<ObservationRunPage />} />
                <Route path="/experiments" element={<ExperimentsPage />} />
                <Route path="/batches" element={<BatchesPage />} />
                <Route path="/batches/:id" element={<BatchPage />} />
                <Route path="/experiments/:id" element={<ExperimentPage />} />
                <Route
                  path="/experiments/:id/cases/:runId"
                  element={<CaseWorkspacePage />}
                />
                <Route path="/comparison" element={<ComparisonPage />} />
                <Route
                  path="/targets"
                  element={<CatalogPage kind="targets" />}
                />
                <Route
                  path="/datasets"
                  element={<CatalogPage kind="datasets" />}
                />
                <Route
                  path="/scorers"
                  element={<CatalogPage kind="scorers" />}
                />
                <Route path="/operations" element={<OperationsPage />} />
                <Route path="/guide" element={<GuidePage />} />
                <Route
                  path="*"
                  element={
                    <div className="not-found">
                      <h1>页面不存在</h1>
                      <NavLink className="button primary" to="/experiments">
                        返回实验
                      </NavLink>
                    </div>
                  }
                />
              </Routes>
            </Suspense>
          </WorkspaceShell>
        </SidebarProvider>
        {quickOpen && (
          <Suspense fallback={<Loading label="正在打开快速导航" />}>
            <QuickNavigation
              onClose={() => setQuickOpen(false)}
              onConnect={() => {
                setQuickOpen(false);
                setConnectionOpen(true);
              }}
            />
          </Suspense>
        )}
        {connectionOpen && (
          <AccountDialog
            account={account}
            onClose={() => setConnectionOpen(false)}
            onLogout={onLogout}
          />
        )}
      </TooltipProvider>
    </ConnectionContext.Provider>
  );
}

function WorkspaceShell({
  api,
  account,
  realtime,
  pageName,
  openConnection,
  quickOpen,
  setQuickOpen,
  children,
}: {
  api: Api | null;
  account: Account;
  realtime: "offline" | "connecting" | "live" | "polling";
  pageName: string;
  openConnection: () => void;
  quickOpen: boolean;
  setQuickOpen: React.Dispatch<React.SetStateAction<boolean>>;
  children: ReactNode;
}) {
  const { isMobile, open, openMobile, setOpenMobile } = useSidebar();
  const location = useLocation();
  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (
        (event.metaKey || event.ctrlKey) &&
        event.key.toLowerCase() === "k" &&
        !event.isComposing
      ) {
        if (
          openMobile ||
          (!quickOpen &&
            document.querySelector('[role="dialog"], dialog[open]'))
        )
          return;
        event.preventDefault();
        setQuickOpen((value) => !value);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [openMobile, quickOpen, setQuickOpen]);
  function connect() {
    setOpenMobile(false);
    openConnection();
  }
  return (
    <>
      <Sidebar collapsible="icon" aria-label="工作空间导航">
        <SidebarHeader className="gap-4 px-3 py-4 group-data-[collapsible=icon]:px-2">
          <div className="flex items-center justify-between gap-2">
            <NavLink
              to="/observe"
              className="console-brand"
              aria-label="Eyes Agent 观测"
              onClick={() => setOpenMobile(false)}
            >
              <Eye />
              <span className="group-data-[collapsible=icon]:hidden">
                eyes.
              </span>
            </NavLink>
            {isMobile && (
              <Button
                variant="ghost"
                size="icon"
                aria-label="关闭导航"
                onClick={() => setOpenMobile(false)}
              >
                <X />
              </Button>
            )}
          </div>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                size="lg"
                variant="outline"
                tooltip="个人账号"
                onClick={connect}
                aria-label="个人账号"
              >
                <UserRound />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span>{account.username}</span>
                  <span className="console-project-note">
                    {
                      {
                        offline: "尚未连接",
                        connecting: "正在连接更新",
                        live: "实时更新已连接",
                        polling: "定期同步 · 实时连接暂不可用",
                      }[realtime]
                    }
                  </span>
                </span>
                <ChevronDown className="ml-auto group-data-[collapsible=icon]:hidden" />
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>
        <SidebarContent>
          <nav aria-label="主导航">
            {navigationGroups.map((group) => (
              <SidebarGroup key={group}>
                <SidebarGroupLabel>{group}</SidebarGroupLabel>
                <SidebarGroupContent>
                  <SidebarMenu>
                    {navigation
                      .filter((item) => item.group === group)
                      .map((item) => (
                        <SidebarMenuItem key={item.url}>
                          <SidebarMenuButton
                            render={<NavLink to={item.url} />}
                            isActive={location.pathname.startsWith(item.url)}
                            tooltip={item.text}
                            onClick={() => setOpenMobile(false)}
                          >
                            <item.icon />
                            <span>{item.text}</span>
                          </SidebarMenuButton>
                        </SidebarMenuItem>
                      ))}
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>
            ))}
          </nav>
        </SidebarContent>
        <SidebarFooter className="px-4 py-4 group-data-[collapsible=icon]:hidden">
          <div className="console-version">
            <span>Eyes Console</span>
            <span>v0.1</span>
          </div>
        </SidebarFooter>
      </Sidebar>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="console-topbar flex h-14 shrink-0 items-center justify-between gap-3 border-b px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <SidebarTrigger
              aria-label={
                isMobile ? "打开导航" : open ? "收起侧栏" : "展开侧栏"
              }
              aria-expanded={isMobile ? openMobile : open}
            />
            <Breadcrumb>
              <BreadcrumbList>
                <BreadcrumbItem className="hidden sm:block">
                  工作空间
                </BreadcrumbItem>
                <BreadcrumbSeparator className="hidden sm:block" />
                <BreadcrumbItem>
                  <BreadcrumbPage>{pageName}</BreadcrumbPage>
                </BreadcrumbItem>
              </BreadcrumbList>
            </Breadcrumb>
          </div>
          <div className="flex shrink-0 items-center gap-2 md:gap-3">
            <Button
              variant="ghost"
              size="icon"
              aria-label="快速导航"
              title="快速导航（⌘ K / Ctrl K）"
              aria-keyshortcuts="Meta+k Control+k"
              onClick={() => setQuickOpen(true)}
            >
              <Search />
            </Button>
            <span
              className="console-connection hidden sm:inline-flex"
              data-connected={!!api}
            >
              <i />
              {api ? "已认证" : "未连接"}
            </span>
            <Button
              variant="outline"
              onClick={openConnection}
              aria-label="账号设置"
            >
              <UserRound data-icon="inline-start" />
              <span className="hidden sm:inline">账号设置</span>
              <span className="sm:hidden">账号</span>
            </Button>
          </div>
        </header>
        <main
          id="main"
          tabIndex={-1}
          className="console-main mx-0 w-full min-w-0 max-w-none px-4 py-6 md:px-6 md:py-7"
        >
          {children}
        </main>
      </div>
    </>
  );
}
function AccountDialog({
  account,
  onClose,
  onLogout,
}: {
  account: Account;
  onClose: () => void;
  onLogout: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function logout() {
    setBusy(true);
    setError(null);
    try {
      await onLogout();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title="个人账号"
      subtitle={`${account.username} 的工作空间`}
      onClose={onClose}
    >
      <div className="form">
        <p>你的 Agent、测试集、实验和观测记录保存在此账号下。</p>
        {error != null && <ErrorNotice error={error} />}
        <div className="form-actions">
          <Button variant="outline" onClick={onClose} disabled={busy}>
            返回工作空间
          </Button>
          <Button onClick={logout} disabled={busy}>
            <LogOut />
            {busy ? "正在退出…" : "退出登录"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
