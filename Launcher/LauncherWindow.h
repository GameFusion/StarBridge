#pragma once
#include <QMainWindow>
#include <QTextEdit>
#include <QPushButton>

#include "ProcessRunner.h"

class LauncherWindow : public QMainWindow
{
    Q_OBJECT
public:
    explicit LauncherWindow(QWidget *parent = nullptr);

    void closeEvent(QCloseEvent *event);

private slots:
    void onPlayClicked();
    void onPauseClicked();
    void onStopClicked();
    void onProcessStateChanged(QProcess::ProcessState state);

private:

    void logMessage(const QString &rawText, bool isError);

    QTextEdit *logView;
    QPushButton *playBtn;
    QPushButton *pauseBtn;
    QPushButton *stopBtn;

    ProcessRunner *runner;

    int maxLogLines = 20000;     // Max history
    int trimLogLines = 15000;     // Max history
    bool autoFollow = false;       // Auto-scroll when new logs arrive
};

