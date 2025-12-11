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
