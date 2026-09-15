/*
 * This code is licensed under the Fastbot license. You may obtain a copy of this license in the LICENSE.txt file in the root directory of this source tree.
 */
/**
 * @authors coverage-diff extension
 */
#ifndef CoverageExporter_H_
#define CoverageExporter_H_

#include "../Base.h"
#include "../model/Graph.h"
#include <map>
#include <string>

namespace fastbotx {

/// Widget-level coverage snapshot exporter (M3 coverage-diff).
///
/// Threading model: the Graph is lock-free, so this exporter never spawns
/// threads and never takes locks. Every entry point runs on the Java decision
/// thread, which is the same thread that mutates the Graph via
/// AiClient.getAction (b0bhkadf); calls are therefore naturally serialized:
///  - observeState() is invoked by Model::getOperateOpt once per decision
///    cycle, while the state still carries widget details (before the
///    DROP_DETAIL_AFTER_SATE eviction clears them);
///  - buildCoverageJson() is invoked from the JNI dumpCoverage entry.
class CoverageExporter {
public:
    /// Record the widgets of the current decision cycle's state. Must be called
    /// on the decision thread while the state widgets still hold details.
    /// \param state the merged state of the current decision cycle
    static void observeState(const StatePtr &state);

    /// Walk the Graph (visited states grouped per activity, visit counts) and
    /// merge the widget quadruples accumulated via observeState() into a
    /// coverage JSON snapshot matching tools/schemas/coverage.schema.json.
    /// \param graph the model graph
    /// \param packageName value written into the JSON "package" field
    /// \return the coverage JSON string
    static std::string
    buildCoverageJson(const GraphPtr &graph, const std::string &packageName);

    /// Clear the accumulated records (used by tests only).
    static void reset();

private:
    struct WidgetRecord {
        std::string resourceID;
        std::string text;
        std::string contentDesc;
        std::string path;
    };

    // keyed by activity name; std::map iteration order is sorted, so repeated
    // exports of the same state produce byte-identical JSON (determinism).
    // Memory bound: deduplicated in observeState()/observeWidgets() by the
    // (activity, widget quadruple) map key, so this accumulator grows with
    // UNIQUE widgets only - acceptable for multi-hour runs.
    static std::map<std::string, std::map<std::string, WidgetRecord> > _activityWidgets;

    static void observeWidgets(const std::string &activity, const WidgetPtrVec &widgets);

    static std::string widgetPath(const WidgetPtr &widget);

    static std::string widgetKey(const WidgetRecord &record);

    static std::string escapeJson(const std::string &value);

    static std::string isoTimestamp();
};

}

#endif // CoverageExporter_H_
