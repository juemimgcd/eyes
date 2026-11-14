import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Search, UserRound, LogOut, X } from "lucide-react";
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
  const [observationScrollPositions] = useState(
    () => new Map<string, number>(),
  );
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
                <Route
                  path="/observe"
                  element={
                    <ObservationPage
                      scrollPositions={observationScrollPositions}
                    />
                  }
                />
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
  account,
  realtime,
  pageName,
  openConnection,
  quickOpen,
  setQuickOpen,
  children,
}: {
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
        <SidebarHeader className="justify-center border-b border-sidebar-border px-4 h-16 group-data-[collapsible=icon]:px-2">
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
        </SidebarHeader>
        <SidebarContent className="pt-3">
          <nav aria-label="主导航">
            {navigationGroups.map((group) => (
              <SidebarGroup
                key={group}
                className="px-3 group-data-[collapsible=icon]:px-2"
              >
                <SidebarGroupLabel className="text-sidebar-foreground/75">
                  {group}
                </SidebarGroupLabel>
                <SidebarGroupContent>
                  <SidebarMenu>
                    {navigation
                      .filter((item) => item.group === group)
                      .map((item) => (
                        <SidebarMenuItem key={item.url}>
                          <SidebarMenuButton
                            render={<NavLink to={item.url} />}
                            isActive={location.pathname.startsWith(item.url)}
                            className="workspace-nav-link gap-2.5 md:h-9 data-active:bg-sidebar-primary data-active:text-sidebar-primary-foreground data-active:font-semibold"
                            aria-label={item.text}
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
        <SidebarFooter className="border-t border-sidebar-border p-3 group-data-[collapsible=icon]:px-2">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                size="lg"
                tooltip="账号设置"
                aria-label={`账号设置，${account.username}`}
                onClick={connect}
              >
                <UserRound />
                <span className="flex min-w-0 flex-1 flex-col group-data-[collapsible=icon]:hidden">
                  <strong className="truncate font-medium">
                    {account.username}
                  </strong>
                  <span className="text-xs opacity-80">个人工作空间</span>
                </span>
                <ChevronRight className="opacity-75 group-data-[collapsible=icon]:hidden" />
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
      </Sidebar>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="console-topbar flex h-16 shrink-0 items-center justify-between gap-3 border-b px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <SidebarTrigger
              aria-label={
                isMobile ? "打开导航" : open ? "收起侧栏" : "展开侧栏"
              }
              aria-expanded={isMobile ? openMobile : open}
            />
            <Breadcrumb aria-label="当前位置">
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
              variant="outline"
              className="console-search-trigger"
              aria-label="快速导航"
              title="快速导航（⌘ K / Ctrl K）"
              aria-keyshortcuts="Meta+k Control+k"
              onClick={() => setQuickOpen(true)}
            >
              <Search />
              <span className="hidden sm:inline">快速导航</span>
              <kbd className="ml-auto hidden sm:inline-flex" aria-hidden="true">
                {navigator.platform.startsWith("Mac") ? "⌘ K" : "Ctrl K"}
              </kbd>
            </Button>
            <span className="console-connection" data-mode={realtime}>
              <i aria-hidden="true" />
              {
                {
                  offline: "未连接",
                  connecting: "连接中",
                  live: "实时同步",
                  polling: "定期同步",
                }[realtime]
              }
            </span>
          </div>
        </header>
        <main id="main" tabIndex={-1} className="console-main">
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
