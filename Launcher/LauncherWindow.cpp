#include "LauncherWindow.h"
#include <QVBoxLayout>
#include <QPushButton>
#include <QApplication>
#include <QLabel>
#include <QScrollBar>
#include <QCloseEvent>
#include <QRegularExpression>

LauncherWindow::LauncherWindow(QWidget *parent)
    : QMainWindow(parent)
{
    setWindowTitle("StarBridge");
    resize(900, 700);

    auto *central = new QWidget(this);
    setCentralWidget(central);
    auto *layout = new QVBoxLayout(central);

    // Title
    auto *title = new QLabel("<h1 style='color:#00d4ff;'>StarBridge</h1><p>Running...</p>");
    title->setAlignment(Qt::AlignCenter);
    layout->addWidget(title);

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

    // Play controller
    QHBoxLayout *controlLayout = new QHBoxLayout();
    controlLayout->setSpacing(12);
    controlLayout->setContentsMargins(20, 10, 20, 20);

    // Play Button
    playBtn = new QPushButton(this);
    playBtn->setIcon(QIcon(":/resources/play.png"));
    playBtn->setFixedSize(48, 48);
    playBtn->setStyleSheet("QPushButton { border-radius: 24px; background: #28a745; }");
    playBtn->setToolTip("Run");
    connect(playBtn, &QPushButton::clicked, this, &LauncherWindow::onPlayClicked);

    // Pause Button
    pauseBtn = new QPushButton(this);
    pauseBtn->setIcon(QIcon(":/resources/pause.png"));
    pauseBtn->setFixedSize(48, 48);
    pauseBtn->setStyleSheet("QPushButton { border-radius: 24px; background: #ffc107; }");
    pauseBtn->setEnabled(false);
    pauseBtn->setToolTip("Pause");
    connect(pauseBtn, &QPushButton::clicked, this, &LauncherWindow::onPauseClicked);

    // Stop Button
    stopBtn = new QPushButton(this);
    stopBtn->setIcon(QIcon(":/resources/stop.png"));
    stopBtn->setFixedSize(48, 48);
    stopBtn->setStyleSheet("QPushButton { border-radius: 24px; background: #dc3545; }");
    stopBtn->setEnabled(false);
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
    runner->startStarBridge();
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

// LauncherWindow.cpp
void LauncherWindow::onPlayClicked()
{
    if (runner->getProcess().state() == QProcess::NotRunning) {
        runner->startStarBridge();
        playBtn->setEnabled(false);
        pauseBtn->setEnabled(true);
        stopBtn->setEnabled(true);
    }
}

void LauncherWindow::onPauseClicked()
{
    if (runner->getProcess().state() == QProcess::Running) {
        runner->getProcess().write("PAUSE\n");  // Send pause signal
        pauseBtn->setEnabled(false);
        playBtn->setEnabled(true);
    }
}

void LauncherWindow::onStopClicked()
{
    runner->stop();
    playBtn->setEnabled(true);
    pauseBtn->setEnabled(false);
    stopBtn->setEnabled(false);
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

// In your logMessage handler — ELITE VERSION
void LauncherWindow::logMessage(const QString &rawText, bool isError)
{
    QString text = rawText;

    // === 1. ESCAPE ALL HTML — THIS IS THE KEY ===
    text = text.toHtmlEscaped();

    // === 2. SAFE DIFF HIGHLIGHTING (using <span> with inline style) ===
    text = text
               // Hunk headers
               .replace(QRegularExpression(R"(^(@@ .*? @@.*)$)"),
                        R"(<span style="color:#79c0ff; font-weight:bold;">\1</span>)")
               // Added lines
               .replace(QRegularExpression(R"(^\+.*$)"),
                        R"(<span style="color:#56d364; background:rgba(86,211,100,0.15);">\0</span>)")
               // Removed lines
               .replace(QRegularExpression(R"(^\-.*$)"),
                        R"(<span style="color:#f85149; background:rgba(248,81,73,0.15);">\0</span>)")
               // Context lines
               .replace(QRegularExpression(R"(^ .*$)"),
                        R"(<span style="color:#8b949e;">\0</span>)")
               // File headers
               .replace(QRegularExpression(R"(^diff --git.*$)"),
                        R"(<span style="color:#8b949e; font-weight:bold;">\0</span>)")
               .replace(QRegularExpression(R"(^index .*$)"),
                        R"(<span style="color:#8b949e;">\0</span>)");

    // === 3. PRESERVE LINE BREAKS ===
    text = text.replace("\n", "<br>");

    // === 4. APPLY LOG LEVEL COLOR ===
    QString color = isError ? "#ff6b6b" : "#e6edf3";
    if (text.contains("ERROR", Qt::CaseInsensitive)) color = "#ff6b6b";
    else if (text.contains("WARNING", Qt::CaseInsensitive)) color = "#ffa657";
    else if (text.contains("INFO", Qt::CaseInsensitive)) color = "#79c0ff";
    else if (text.contains("DEBUG", Qt::CaseInsensitive)) color = "#8b949e";

    // === 5. INSERT WITH SAFE STYLING ===
    logView->append(QString("<span style='color:%1; font-family:Menlo,monospace;'>%2</span>")
                        .arg(color, text));

    // === MAX HISTORY ENFORCEMENT ===
    QTextDocument *doc = logView->document();
    int blockCount = doc->blockCount();

    QTextCursor cursor = logView->textCursor();
    cursor.movePosition(QTextCursor::Start);

    if (blockCount > maxLogLines) {
        cursor.movePosition(QTextCursor::Down, QTextCursor::KeepAnchor, blockCount - trimLogLines);
        cursor.removeSelectedText();
        cursor.deleteChar(); // remove extra newline
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
    qApp->quit();
}
