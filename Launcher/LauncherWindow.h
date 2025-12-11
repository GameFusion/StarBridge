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
    QTextEdit *logView;
    QPushButton *playBtn;
    QPushButton *pauseBtn;
    QPushButton *stopBtn;

    ProcessRunner *runner;
    //QProcess *pythonProcess;
};

