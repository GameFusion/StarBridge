#include "LauncherWindow.h"
#include <QHBoxLayout>
#include <QVBoxLayout>
#include <QPushButton>
#include <QApplication>
#include <QLabel>
#include <QScrollBar>
#include <QCloseEvent>
#include <QRegularExpression>
#include <QFontDatabase>
#include <QTimer>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QIcon>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QSignalBlocker>

LauncherWindow::LauncherWindow(QWidget *parent)
    : QMainWindow(parent)
{
    setWindowTitle("StarBridge Launcher");
    setWindowIcon(QIcon(":/resources/StarBridge.png"));
    resize(900, 700);

    // In LauncherWindow constructor — after creating central widget
    QWidget *central = new QWidget(this);
    setCentralWidget(central);

    // === ELITE: Perfect deep-space dark background ===
    central->setStyleSheet("background-color: #0a0f1a;");

    // Optional: Make everything look elite
    setStyleSheet(R"(
    QMainWindow {
        background-color: #0a0f1a;
    }
QTextEdit {
        background-color: #0f1117;
        color: #e6edf3;
        border: none;
        border-radius: 12px;
        padding: 16px;
        font-family: 'Menlo', 'Consolas', 'Courier New', monospace;
        font-size: 11px;
        line-height: 1.5;
    }

    /* === ELITE MINIMAL SCROLLBAR === */
    QScrollBar::vertical {
        background: green;
        width: 10px;
        margin: 0px;
        border-radius: 5px;
        border: none;
    }

    QScrollBar::handle:vertical {
        background: rgba(255, 255, 255, 0.3);
        min-height: 30px;
        border-radius: 5px;
        border: none;
    }

    QScrollBar::handle:vertical:hover {
        background: rgba(255, 255, 255, 0.5);
    }

    QScrollBar::handle:vertical:pressed {
        background: rgba(255, 255, 255, 0.7);
    }

    /* Hide arrows — the elite way */
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {
        height: 0px;
        width: 0px;
        border: none;
        background: none;
border-radius: 5px;
    }

    QScrollBar::add-page:vertical,
    QScrollBar::sub-page:vertical {
        background: none;
    }
    QPushButton {
        background: transparent;
        color: white;
        border: 2px solid;
        border-radius: 24px;
        font-weight: bold;
    }
)");



    //auto *central = new QWidget(this);
    //setCentralWidget(central);
    auto *layout = new QVBoxLayout(central);

    // Font Awesome
    int fontId = QFontDatabase::addApplicationFont(":/resources/fa-solid-900.ttf");
    if (fontId != -1) {
        QStringList fontFamilies = QFontDatabase::applicationFontFamilies(fontId);
        if (!fontFamilies.isEmpty()) {
            qDebug() << "Font Awesome Solid loaded:" << fontFamilies;
        }
    }

    QFont fa = QFont("Font Awesome 5 Free", 40);

    // Repository filter (overlay)
    filterBtn = new QToolButton(central);
    filterBtn->setToolTip("Filter repositories");
    filterBtn->setPopupMode(QToolButton::InstantPopup);
    filterBtn->setAutoRaise(true);
    filterBtn->setText(QChar(0xf0b0)); // fa-filter
    filterBtn->setFont(QFont("Font Awesome 5 Free", 11));
    filterBtn->setFixedSize(30, 20);
    filterBtn->setStyleSheet(
        "QToolButton { color:#8b949e; border:none; padding-left:1px; padding-right:12px; }"
        "QToolButton:hover { color:#e6edf3; }"
        "QToolButton::menu-indicator { subcontrol-origin: padding; subcontrol-position: right center; }"
    );

    filterMenu = new QMenu(this);
    allReposAction = new QAction("All repositories", this);
    allReposAction->setCheckable(true);
    allReposAction->setChecked(true);
    filterMenu->addAction(allReposAction);

    loadRepositoryFilters();
    if (!repositoryNames.isEmpty()) {
        filterMenu->addSeparator();
    }
    for (const QString &repoName : repositoryNames) {
        QAction *repoAction = new QAction(repoName, this);
        repoAction->setCheckable(true);
        repoAction->setChecked(true);
        filterMenu->addAction(repoAction);
        repoFilterActions.append(repoAction);
    }

    connect(filterMenu, &QMenu::triggered, this, &LauncherWindow::onFilterActionTriggered);
    filterBtn->setMenu(filterMenu);

    if (repositoryNames.isEmpty()) {
        filterBtn->setEnabled(false);
        filterBtn->setToolTip("No repositories found in settings.json");
    }

    // Title
    //auto *title = new QLabel("<h1 style='color:#00d4ff;'>StarBridge</h1><p>Running...</p>");
    statusLabel = new QLabel("<h1 style='color:#00d4ff;'>StarBridge</h1><p>Checking...</p>");
    statusLabel->setAlignment(Qt::AlignCenter);
    layout->addWidget(statusLabel);

    // === ELITE: Health check timer — every 5 seconds ===
    healthTimer = new QTimer(this);
    connect(healthTimer, &QTimer::timeout, this, &LauncherWindow::checkHealth);
    healthTimer->start(5000); // 5s interval

    // Log view
    logView = new QTextEdit(this);
    logView->setReadOnly(true);
    logView->setFont(QFont("Menlo", 11));
    logView->setStyleSheet(R"(
        QTextEdit {
            background: #000;
            color: #0f0;
            border: none;
            padding: 10px;
        }
    )");
    layout->addWidget(logView);
    QTimer::singleShot(0, this, [this]() { positionFilterOverlay(); });

    // Play controller
    QHBoxLayout *controlLayout = new QHBoxLayout();
    controlLayout->setSpacing(12);
    controlLayout->setContentsMargins(20, 10, 20, 20);

    // Hover/press animation ===
    QString baseStyle = R"(
    QPushButton {
        border-radius: 24px;
        background: transparent;
        color: %1;
        font-size: 30px;
        border: 0px solid %1;
        transition: all 0.2s ease;
    }
    QPushButton:hover {
        color: white;
        background: transparent;
        font-size: 34px;
        transform: scale(1.1);
        box-shadow: 0 0 20px %1;
    }
    QPushButton:pressed {
        font-size: 36px;
        transform: scale(1.2);
        box-shadow: 0 0 30px %1;
    }
    QPushButton:disabled {
        color: #555;
        border-color: #555;
        background: transparent;
    }
)";

    // Play Button — GREEN
    playBtn = new QPushButton(this);
    playBtn->setFont(fa);
    playBtn->setText(QChar(0xf04b)); // fa-play
    playBtn->setFixedSize(40, 40);
    playBtn->setStyleSheet(baseStyle.arg("#28a745"));
    playBtn->setToolTip("Run");
    connect(playBtn, &QPushButton::clicked, this, &LauncherWindow::onPlayClicked);

    // Pause Button — AMBER
    pauseBtn = new QPushButton(this);
    pauseBtn->setFont(fa);
    pauseBtn->setText(QChar(0xf28b)); // fa-pause
    pauseBtn->setFixedSize(40, 40);
    pauseBtn->setStyleSheet(baseStyle.arg("#ffc107") + " color: black;");
    pauseBtn->setToolTip("Pause");
    connect(pauseBtn, &QPushButton::clicked, this, &LauncherWindow::onPauseClicked);

    // Stop Button — RED
    stopBtn = new QPushButton(this);
    stopBtn->setFont(fa);
    stopBtn->setText(QChar(0xf28d)); // fa-stop
    stopBtn->setFixedSize(40, 40);
    stopBtn->setStyleSheet(baseStyle.arg("#dc3545"));
    stopBtn->setToolTip("Stop");
    connect(stopBtn, &QPushButton::clicked, this, &LauncherWindow::onStopClicked);

    controlLayout->addStretch();
    controlLayout->addWidget(stopBtn);
    controlLayout->addWidget(pauseBtn);
    controlLayout->addWidget(playBtn);
    controlLayout->addStretch();

    layout->addLayout(controlLayout);

    // Start process
    runner = new ProcessRunner(this);
    connect(runner, &ProcessRunner::logMessage, this, [this](const QString &msg, bool isError) {

        logMessage(msg, isError);
/*
        QTextCharFormat fmt;
        fmt.setForeground(isError ? Qt::red : Qt::green);
        logView->setCurrentCharFormat(fmt);
        logView->append(msg);
        logView->verticalScrollBar()->setValue(logView->verticalScrollBar()->maximum());
*/

    });

    // In constructor — after creating logView
    connect(logView->verticalScrollBar(), &QScrollBar::valueChanged, this, [this]() {
        QScrollBar *sb = logView->verticalScrollBar();
        bool nearBottom = (sb->maximum() - sb->value()) <= 100;

        // Only disable auto-follow if user manually scrolled up
        if (!nearBottom && autoFollow) {
            autoFollow = false;
            // Optional: show indicator
            // statusBar()->showMessage("Auto-follow disabled");
        }
    });



    //
    // ===  SYSTEM TRAY ===
    trayIcon = new QSystemTrayIcon(this);
    trayIcon->setIcon(QIcon(":/resources/StarBridge.png"));
    trayIcon->setToolTip("StarBridge");

    QIcon icon(":/resources/helloServer.svg");
    trayIcon->setIcon(icon);


    trayMenu = new QMenu(this);

    showAction = new QAction("Show Log Window", this);
    quitAction = new QAction("Quit StarBridge", this);

    connect(showAction, &QAction::triggered, this, &LauncherWindow::onShowWindow);
    connect(quitAction, &QAction::triggered, this, &LauncherWindow::onQuit);

    trayMenu->addAction(showAction);
    trayMenu->addSeparator();
    trayMenu->addAction(quitAction);

    trayIcon->setContextMenu(trayMenu);
    trayIcon->show();

    // Optional: Click tray icon = show window
    connect(trayIcon, &QSystemTrayIcon::activated, this, [this](QSystemTrayIcon::ActivationReason reason) {
        if (reason == QSystemTrayIcon::Trigger || reason == QSystemTrayIcon::DoubleClick) {
            onShowWindow();
        }
    });

    // ---
    onPlayClicked();
}

LauncherWindow::~LauncherWindow()
{

    runner->stop();  // your stop() method that kills the process

    //  Clean up tray icon ===
    if (trayIcon) {
        trayIcon->hide();
        trayIcon->deleteLater();
    }

    delete runner;

    qDebug() << "Launcher shutdown complete — goodbye!";
}

void LauncherWindow::closeEvent(QCloseEvent *event)
{
    if (trayIcon->isVisible()) {
        hide();  // Hide window, stay in tray
        event->ignore();
    }

    //runner->stop();
    //event->accept();
}

void LauncherWindow::resizeEvent(QResizeEvent *event)
{
    QMainWindow::resizeEvent(event);
    positionFilterOverlay();
}

void LauncherWindow::positionFilterOverlay()
{
    if (!filterBtn || !logView) {
        return;
    }

    const QRect logRect = logView->geometry();
    const int rightPadding = 8;
    const int gapAboveLog = 2;

    int x = logRect.right() - filterBtn->width() - rightPadding;
    int y = logRect.top() - filterBtn->height() - gapAboveLog;

    x = qMax(0, x);
    y = qMax(0, y);

    filterBtn->move(x, y);
    filterBtn->raise();
}


void LauncherWindow::onPlayClicked()
{
    State = Play;
    if (runner->getProcess().state() == QProcess::NotRunning && !runner->isLaunchInProgress()) {
        emit logMessage("Play clicked — starting StarBridge...", false);
        runner->startStarBridge();

        playBtn->setEnabled(false);
        pauseBtn->setEnabled(true);
        stopBtn->setEnabled(true);
    } else {
        emit logMessage("Play clicked — but already running or launching", false);
    }

    QTimer::singleShot(100, this, &LauncherWindow::checkHealth);
}

void LauncherWindow::onPauseClicked()
{
    State = Pause;
    if (runner->getProcess().state() == QProcess::Running) {
        emit logMessage("Pause clicked — sending PAUSE signal...", false);
        runner->getProcess().write("PAUSE\n");

        pauseBtn->setEnabled(false);
        playBtn->setEnabled(true);
    } else {
        emit logMessage("Pause clicked — but process not running", false);
    }

    checkHealth();
}

void LauncherWindow::onStopClicked()
{
    State = Stop;
    emit logMessage("Stop clicked — terminating StarBridge...", false);
    runner->stop();

    playBtn->setEnabled(true);
    pauseBtn->setEnabled(false);
    stopBtn->setEnabled(false);

    checkHealth();
}

void LauncherWindow::onProcessStateChanged(QProcess::ProcessState state)
{
    switch (state) {
    case QProcess::NotRunning:
        playBtn->setEnabled(true);
        pauseBtn->setEnabled(false);
        stopBtn->setEnabled(false);
        break;
    case QProcess::Starting:
        break;
    case QProcess::Running:
        playBtn->setEnabled(false);
        pauseBtn->setEnabled(true);
        stopBtn->setEnabled(true);
        break;
    }
}

void LauncherWindow::loadRepositoryFilters()
{
    repositoryNames.clear();
    enabledRepositories.clear();

    QFile file(QDir::currentPath() + "/settings.json");
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return;
    }

    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll());
    if (!doc.isObject()) {
        return;
    }

    const QJsonArray repos = doc.object().value("repositories").toArray();
    QSet<QString> seen;
    for (const QJsonValue &v : repos) {
        if (!v.isString()) {
            continue;
        }

        const QString repoPath = v.toString().trimmed();
        if (repoPath.isEmpty()) {
            continue;
        }

        QString repoName = QFileInfo(repoPath).fileName();
        if (repoName.isEmpty()) {
            repoName = repoPath;
        }
        if (!seen.contains(repoName)) {
            seen.insert(repoName);
            repositoryNames.append(repoName);
        }
    }

    repositoryNames.sort(Qt::CaseInsensitive);
    for (const QString &repoName : repositoryNames) {
        enabledRepositories.insert(repoName);
    }
}

bool LauncherWindow::shouldDisplayLogMessage(const QString &rawText) const
{
    if (!allReposAction || allReposAction->isChecked() || repositoryNames.isEmpty()) {
        return true;
    }

    bool mentionsAnyRepo = false;
    for (const QString &repoName : repositoryNames) {
        if (rawText.contains(repoName, Qt::CaseInsensitive)) {
            mentionsAnyRepo = true;
            if (enabledRepositories.contains(repoName)) {
                return true;
            }
        }
    }

    // Keep global/system logs visible even when repo filter is active.
    return !mentionsAnyRepo;
}

QString LauncherWindow::formatLogMessageHtml(const QString &rawText, bool isError) const
{
    QString text = rawText;

    text = text.toHtmlEscaped();

    text = text
               .replace(QRegularExpression(R"(^(@@ .*? @@.*)$)"),
                        R"(<span style="color:#79c0ff; font-weight:bold;">\1</span>)")
               .replace(QRegularExpression(R"(^\+.*$)"),
                        R"(<span style="color:#56d364; background:rgba(86,211,100,0.15);">\0</span>)")
               .replace(QRegularExpression(R"(^\-.*$)"),
                        R"(<span style="color:#f85149; background:rgba(248,81,73,0.15);">\0</span>)")
               .replace(QRegularExpression(R"(^ .*$)"),
                        R"(<span style="color:#8b949e;">\0</span>)")
               .replace(QRegularExpression(R"(^diff --git.*$)"),
                        R"(<span style="color:#8b949e; font-weight:bold;">\0</span>)")
               .replace(QRegularExpression(R"(^index .*$)"),
                        R"(<span style="color:#8b949e;">\0</span>)");

    text = text.replace("\n", "<br>");

    QString color = isError ? "#ff6b6b" : "#e6edf3";
    if (text.contains("ERROR", Qt::CaseInsensitive)) color = "#ff6b6b";
    else if (text.contains("WARNING", Qt::CaseInsensitive)) color = "#ffa657";
    else if (text.contains("INFO", Qt::CaseInsensitive)) color = "#79c0ff";
    else if (text.contains("DEBUG", Qt::CaseInsensitive)) color = "#8b949e";

    return QString("<span style='color:%1; font-family:Menlo,monospace;'>%2</span>")
        .arg(color, text);
}

void LauncherWindow::refreshLogView()
{
    logView->clear();
    for (const auto &entry : logEntries) {
        if (shouldDisplayLogMessage(entry.first)) {
            logView->append(formatLogMessageHtml(entry.first, entry.second));
        }
    }

    if (autoFollow) {
        logView->verticalScrollBar()->setValue(logView->verticalScrollBar()->maximum());
    }
}

void LauncherWindow::onFilterActionTriggered(QAction *action)
{
    if (!action) {
        return;
    }

    QSignalBlocker blocker(filterMenu);

    if (action == allReposAction) {
        if (!allReposAction->isChecked()) {
            allReposAction->setChecked(true);
            return;
        }
        enabledRepositories.clear();
        for (QAction *repoAction : repoFilterActions) {
            repoAction->setChecked(true);
            enabledRepositories.insert(repoAction->text());
        }
    } else {
        enabledRepositories.clear();
        int checkedCount = 0;
        for (QAction *repoAction : repoFilterActions) {
            if (repoAction->isChecked()) {
                ++checkedCount;
                enabledRepositories.insert(repoAction->text());
            }
        }

        if (!repoFilterActions.isEmpty() && checkedCount == repoFilterActions.size()) {
            allReposAction->setChecked(true);
        } else {
            allReposAction->setChecked(false);
        }

        if (!repoFilterActions.isEmpty() && checkedCount == 0) {
            allReposAction->setChecked(true);
            enabledRepositories.clear();
            for (QAction *repoAction : repoFilterActions) {
                repoAction->setChecked(true);
                enabledRepositories.insert(repoAction->text());
            }
        }
    }

    refreshLogView();
}

void LauncherWindow::logMessage(const QString &rawText, bool isError)
{
    logEntries.append(qMakePair(rawText, isError));

    // Keep history bounded and refresh if old entries are trimmed.
    if (logEntries.size() > maxLogLines) {
        while (logEntries.size() > trimLogLines) {
            logEntries.removeFirst();
        }
        refreshLogView();
        return;
    }

    if (shouldDisplayLogMessage(rawText)) {
        logView->append(formatLogMessageHtml(rawText, isError));
        if (autoFollow) {
            logView->verticalScrollBar()->setValue(logView->verticalScrollBar()->maximum());
        }
    }
}

// Show window from tray
void LauncherWindow::onShowWindow()
{
    show();
    raise();
    activateWindow();
}

// Real quit
void LauncherWindow::onQuit()
{
    runner->stop();  // Kill Python process
    //delete runner;
    qApp->quit();
}

void LauncherWindow::checkHealth()
{
    QNetworkAccessManager *manager = new QNetworkAccessManager(this);
    connect(manager, &QNetworkAccessManager::sslErrors,
            [](QNetworkReply *r, const QList<QSslError>&) { r->ignoreSslErrors(); });

    QString backendUrl = QString("https://127.0.0.1:%1").arg(runner->getPort());
    QNetworkRequest request(QString("%1/health").arg(backendUrl));
    auto *reply = manager->get(request);

    QTimer::singleShot(3000, reply, [reply]() { if (!reply->isFinished()) reply->abort(); });

    connect(reply, &QNetworkReply::finished, this, [this, reply, manager, backendUrl]() {
        QString statusText = "Stopped";
        QString statusColor = "#dc3545";
        QString backendPort = runner->getPort();
        QString frontendLink = "";
        QString frontendPort = "";

        if (reply->error() == QNetworkReply::NoError) {
            QJsonDocument doc = QJsonDocument::fromJson(reply->readAll());
            QJsonObject obj = doc.object();

            if (obj["status"].toString() == "healthy") {
                statusText = "Running";
                statusColor = "#28a745";

                auto ports = obj["ports"].toObject();
                backendPort = QString::number(ports["backend"].toInt());
                frontendPort = QString::number(ports["frontend"].toInt());
                frontendLink = ports["frontend_url"].toString();
            }
        }

        // === ELITE: Beautiful dual-port status with clickable frontend ===
        QString html = QString(
                           "<h1 style='color:#00d4ff; margin:0;'>StarBridge</h1>"
                           "<p style='color:%1; margin:4px 0 8px 0; font-size:12px; font-weight:bold;'>%2</p>"
                           "<div style='color:#888; font-size:10px; line-height:1.6;'>"
                           "   Backend: <strong>%3</strong>"
                           "   Frontend: %4"
                           "</div>"
                           )
                           .arg(statusColor)
                           .arg(statusText)
                           .arg(backendUrl)
                           .arg(frontendLink.isEmpty()
                                    ? QString("<strong>%1</strong> (offline)").arg(frontendPort)
                                    : QString("<a href='%1' style='color:#1e90ff; text-decoration:underline;'>%1 →</a>").arg(frontendLink)
                                );

        statusLabel->setText(html);
        statusLabel->setOpenExternalLinks(true); // ← This makes the link clickable!

        reply->deleteLater();
        manager->deleteLater();

        if (this->State == Play &&
            statusText == "Stopped" &&
            runner->getProcess().state() == QProcess::NotRunning &&
            !runner->isLaunchInProgress()) {
            emit logMessage("Relaunching StarBridge service...", false);
            onPlayClicked();
        }
    });
}

void LauncherWindow::updateStatus(const QString &text, const QString &color)
{
    QString html = QString(
                       "<h1 style='color:#00d4ff; margin:0;'>StarBridge</h1>"
                       "<p style='color:%1; margin:4px 0 0 0; font-size:18px; font-weight:bold;'>%2</p>"
                       ).arg(color, text);

    if (text == "Running") {
        html += "<small style='color:#666;'>Port " + runner->getPort() + " • Online</small>";
    } else {
        html += "<small style='color:#888;'>Port " + runner->getPort() + " • Offline</small>";
    }

    statusLabel->setText(html);
}

