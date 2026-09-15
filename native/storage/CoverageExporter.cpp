/*
 * This code is licensed under the Fastbot license. You may obtain a copy of this license in the LICENSE.txt file in the root directory of this source tree.
 */
/**
 * @authors coverage-diff extension
 */
#include "CoverageExporter.h"

#include "State.h"
#include "Widget.h"

#include <cstdio>
#include <ctime>
#include <sstream>
#include <vector>

namespace fastbotx {

    std::map<std::string, std::map<std::string, CoverageExporter::WidgetRecord> >
            CoverageExporter::_activityWidgets;

    void CoverageExporter::observeState(const StatePtr &state) {
        if (nullptr == state) {
            return;
        }
        stringPtr activity = state->getActivityString();
        if (nullptr == activity) {
            return;
        }
        observeWidgets(*(activity.get()), state->getWidgets());
    }
    void CoverageExporter::observeWidgets(const std::string &activity, const WidgetPtrVec &widgets) {
        std::map<std::string, WidgetRecord> &widgetMap = _activityWidgets[activity];
        for (const auto &widget: widgets) {
            if (nullptr == widget) {
                continue;
            }
            WidgetRecord record;
            record.resourceID = widget->getResourceID();
            record.text = widget->getText();
            record.contentDesc = widget->getContextDesc();
            record.path = widgetPath(widget);
            if (record.resourceID.empty() && record.text.empty()
                && record.contentDesc.empty() && record.path.empty()) {
                continue;
            }
            widgetMap[widgetKey(record)] = record;
        }
    }

    std::string CoverageExporter::widgetPath(const WidgetPtr &widget) {
        std::vector<std::string> segments;
        WidgetPtr cursor = widget;
        while (nullptr != cursor) {
            segments.emplace_back(cursor->getClazz());
            cursor = cursor->getParent();
        }
        std::string path;
        for (auto iterator = segments.rbegin(); iterator != segments.rend(); ++iterator) {
            if (!path.empty()) {
                path += "/";
            }
            path += *iterator;
        }
        return path;
    }

    std::string CoverageExporter::widgetKey(const WidgetRecord &record) {
        std::string key = record.resourceID;
        key += '\x1F';
        key += record.text;
        key += '\x1F';
        key += record.contentDesc;
        key += '\x1F';
        key += record.path;
        return key;
    }

    std::string CoverageExporter::escapeJson(const std::string &value) {
        std::string out;
        out.reserve(value.size() + 8);
        for (size_t i = 0; i < value.size(); ++i) {
            unsigned char c = (unsigned char) value[i];
            if (c == '"') {
                out += '\x5C';
                out += '"';
            } else if (c == '\x5C') {
                out += '\x5C';
                out += '\x5C';
            } else if (c == '\b') {
                out += '\x5C';
                out += 'b';
            } else if (c == '\f') {
                out += '\x5C';
                out += 'f';
            } else if (c == '\n') {
                out += '\x5C';
                out += 'n';
            } else if (c == '\r') {
                out += '\x5C';
                out += 'r';
            } else if (c == '\t') {
                out += '\x5C';
                out += 't';
            } else if (c < 0x20) {
                char escape[8];
                snprintf(escape, sizeof(escape), "\x5Cu%04X", c);
                out += escape;
            } else {
                out += (char) c;
            }
        }
        return out;
    }

    std::string CoverageExporter::isoTimestamp() {
        time_t now = time(nullptr);
        struct tm utc;
        gmtime_r(&now, &utc);
        char buffer[32];
        snprintf(buffer, sizeof(buffer), "%04d-%02d-%02dT%02d:%02d:%02dZ",
                 utc.tm_year + 1900, utc.tm_mon + 1, utc.tm_mday,
                 utc.tm_hour, utc.tm_min, utc.tm_sec);
        return std::string(buffer);
    }

    std::string
    CoverageExporter::buildCoverageJson(const GraphPtr &graph, const std::string &packageName) {
        std::map<std::string, long> activityVisits;
        if (nullptr != graph) {
            for (const auto &state: graph->getStates()) {
                stringPtr activity = state->getActivityString();
                if (nullptr == activity) {
                    continue;
                }
                activityVisits[*(activity.get())] += (long) state->getVisitedCount();
            }
        }
        for (const auto &entry: _activityWidgets) {
            if (activityVisits.find(entry.first) == activityVisits.end()) {
                activityVisits[entry.first] = 0;
            }
        }

        std::ostringstream json;
        json << "{";
        json << "\"version\":\"\",";
        json << "\"package\":\"" << escapeJson(packageName) << "\",";
        json << "\"captured_at\":\"" << isoTimestamp() << "\",";
        json << "\"activities\":[";
        bool firstActivity = true;
        for (const auto &activityEntry: activityVisits) {
            if (!firstActivity) {
                json << ",";
            }
            firstActivity = false;
            json << "{\"name\":\"" << escapeJson(activityEntry.first) << "\",";
            json << "\"visit_count\":" << activityEntry.second << ",";
            json << "\"widgets\":[";
            auto found = _activityWidgets.find(activityEntry.first);
            if (found != _activityWidgets.end()) {
                bool firstWidget = true;
                for (const auto &widgetEntry: found->second) {
                    if (!firstWidget) {
                        json << ",";
                    }
                    firstWidget = false;
                    const WidgetRecord &record = widgetEntry.second;
                    json << "{\"resource_id\":";
                    json << (record.resourceID.empty() ? "null"
                            : ("\"" + escapeJson(record.resourceID) + "\""));
                    json << ",\"text\":";
                    json << (record.text.empty() ? "null"
                            : ("\"" + escapeJson(record.text) + "\""));
                    json << ",\"content_desc\":";
                    json << (record.contentDesc.empty() ? "null"
                            : ("\"" + escapeJson(record.contentDesc) + "\""));
                    json << ",\"path\":\"" << escapeJson(record.path) << "\"}";
                }
            }
            json << "]}";
        }
        json << "]}";
        return json.str();
    }

    void CoverageExporter::reset() {
        _activityWidgets.clear();
    }

} // namespace fastbotx
