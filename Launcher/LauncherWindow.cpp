#include "LauncherWindow.h"
#include <QVBoxLayout>
#include <QPushButton>
#include <QApplication>
#include <QLabel>
#include <QScrollBar>
#include <QCloseEvent>

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
        QTextCharFormat fmt;
        fmt.setForeground(isError ? Qt::red : Qt::green);
        logView->setCurrentCharFormat(fmt);
        logView->append(msg);
        logView->verticalScrollBar()->setValue(logView->verticalScrollBar()->maximum());
    });

    runner->startStarBridge();
}

void LauncherWindow::closeEvent(QCloseEvent *event)
{
    runner->stop();
    event->accept();
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
