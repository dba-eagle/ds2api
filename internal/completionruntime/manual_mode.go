package completionruntime

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"ds2api/internal/assistantturn"
	"ds2api/internal/config"
	"ds2api/internal/promptcompat"
)

var (
	manualWorkDir      = filepath.Join(os.Getenv("PWD"), ".ds2api_manual")
	manualRequestFile  = filepath.Join(manualWorkDir, "request.json")
	manualPromptFile   = filepath.Join(manualWorkDir, "prompt.txt")
	manualResponseFile = filepath.Join(manualWorkDir, "response.txt")

	// 串行化手动模式：一次只处理一个请求，避免文件竞争
	manualModeMutex sync.Mutex
	// 请求计数器，用于日志标识
	manualRequestCounter int
	counterMutex         sync.Mutex
)

func isManualMode() bool {
	return os.Getenv("DS2API_MANUAL_MODE") == "true"
}

func manualModeStart(ctx context.Context, stdReq promptcompat.StandardRequest) (StartResult, *assistantturn.OutputError) {
	// 串行化：一次只处理一个手动请求，其他请求在这里排队
	manualModeMutex.Lock()
	defer manualModeMutex.Unlock()

	counterMutex.Lock()
	manualRequestCounter++
	reqNum := manualRequestCounter
	counterMutex.Unlock()

	if err := os.MkdirAll(manualWorkDir, 0755); err != nil {
		return StartResult{Request: stdReq}, &assistantturn.OutputError{
			Status: http.StatusInternalServerError, Message: err.Error(), Code: "error",
		}
	}

	sessionID := fmt.Sprintf("manual-%s-%03d", time.Now().Format("20060102-150405"), reqNum)
	payload := stdReq.CompletionPayload(sessionID)

	if err := manualModeOutputRequest(reqNum, payload, stdReq.FinalPrompt); err != nil {
		return StartResult{Request: stdReq}, &assistantturn.OutputError{
			Status: http.StatusInternalServerError, Message: err.Error(), Code: "error",
		}
	}

	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] 等待用户输入... 轮询 %s", reqNum, manualResponseFile))

	respText, err := manualModeWaitForResponse(ctx, reqNum)
	if err != nil {
		return StartResult{Request: stdReq}, &assistantturn.OutputError{
			Status: http.StatusInternalServerError, Message: err.Error(), Code: "error",
		}
	}

	resp := manualModeCreateResponse(respText)
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] 已收到回复，返回给客户端", reqNum))
	return StartResult{
		SessionID: sessionID,
		Payload:   payload,
		Response:  resp,
		Request:   stdReq,
	}, nil
}

func manualModeOutputRequest(reqNum int, payload map[string]any, prompt string) error {
	reqData, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(manualRequestFile, reqData, 0644); err != nil {
		return err
	}
	if err := os.WriteFile(manualPromptFile, []byte(prompt), 0644); err != nil {
		return err
	}

	preview := prompt
	if len(preview) > 120 {
		preview = preview[:120] + "..."
	}
	preview = strings.ReplaceAll(preview, "\n", " ")

	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] ════════════════════════════════════════════════════", reqNum))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] 手动模式已激活，请求已捕获", reqNum))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] Prompt预览: %s", reqNum, preview))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] 操作步骤：", reqNum))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d]   1. 查看完整prompt：cat %s", reqNum, manualPromptFile))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d]   2. 复制到网页端发送", reqNum))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d]   3. 复制网页端回复", reqNum))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d]   4. 回灌：cat > %s  （粘贴后按Ctrl+D）", reqNum, manualResponseFile))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] 服务正在轮询等待...", reqNum))
	config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] ════════════════════════════════════════════════════", reqNum))
	return nil
}

func manualModeWaitForResponse(ctx context.Context, reqNum int) (string, error) {
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] 请求被取消（客户端断开或超时），释放锁给下一个请求", reqNum))
			return "", ctx.Err()
		case <-ticker.C:
			// 直接尝试读取，成功则删除返回
			// 避免 Stat + Read 两步之间的竞争条件
			data, err := os.ReadFile(manualResponseFile)
			if err == nil && len(data) > 0 {
				if rmErr := os.Remove(manualResponseFile); rmErr != nil {
					config.Logger.Warn(fmt.Sprintf("[MANUAL_MODE #%d] 删除response文件失败: %v", reqNum, rmErr))
				}
				config.Logger.Info(fmt.Sprintf("[MANUAL_MODE #%d] 成功读取回复 (%d 字节)", reqNum, len(data)))
				return string(data), nil
			}
		}
	}
}

func manualModeCreateResponse(text string) *http.Response {
	var lines []string

	thinkOpen := " 	"
	thinkClose := " 	"

	thinkStart := strings.Index(text, thinkOpen)
	thinkEnd := strings.Index(text, thinkClose)

	if thinkStart >= 0 && thinkEnd > thinkStart {
		thinking := text[thinkStart+len(thinkOpen) : thinkEnd]
		before := text[:thinkStart]
		after := text[thinkEnd+len(thinkClose):]
		content := strings.TrimSpace(before + after)

		if strings.TrimSpace(thinking) != "" {
			lines = append(lines, fmt.Sprintf(`data: {"p":"response/thinking_content","v":%q}`, thinking))
		}
		if content != "" {
			lines = append(lines, fmt.Sprintf(`data: {"p":"response/content","v":%q}`, content))
		}
	} else if strings.TrimSpace(text) != "" {
		lines = append(lines, fmt.Sprintf(`data: {"p":"response/content","v":%q}`, text))
	}

	lines = append(lines, `data: {"p":"response/status","v":"FINISHED"}`)
	lines = append(lines, "data: [DONE]")
	lines = append(lines, "") // trailing newline

	body := strings.Join(lines, "\n")

	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       io.NopCloser(strings.NewReader(body)),
		Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
	}
}
